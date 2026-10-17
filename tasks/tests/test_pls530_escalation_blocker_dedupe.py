"""PLS-530: operator escalation blocker pre-mint dedupe and classification."""

import threading
import uuid
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase

import tasks.services.escalation_routing as escalation_routing
from tasks.models import (
    EscalationBlockerDependency,
    EscalationRoutingDisposition,
    Item,
    ItemLink,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.escalation_routing import (
    _Plan,
    _prepare_required_blocker,
    render_escalation_blocker_metrics,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


class Pls530EscalationBlockerDedupeTests(TransactionTestCase):
    """Exercise two top-level parents through the real typed routing sequence."""

    def setUp(self):
        user_model = get_user_model()
        self.actor = user_model.objects.create_user(
            username="pls530-actor",
            email="pls530-actor@example.com",
            is_staff=True,
        )
        self.admin_owner = user_model.objects.create_user(
            username="pls530-admin",
            email="pls530-admin@example.com",
        )
        self.ceo_decision_owner = user_model.objects.create_user(
            username="pls530-ceo-decisions",
            email="pls530-ceo-decisions@example.com",
        )
        self.engineer = user_model.objects.create_user(
            username="pls530-engineer",
            email="pls530-engineer@example.com",
        )
        self.devops_owner = user_model.objects.create_user(
            username="pls905-devops",
            email="pls905-devops@example.com",
        )
        self.merge_owner = user_model.objects.create_user(
            username="pls905-merge",
            email="pls905-merge@example.com",
        )
        self.project = Project.objects.create(name="PLS-530", key="P530", org_id=1)
        open_status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        blocked_status, _ = Status.objects.get_or_create(
            slug="blocked", defaults={"name": "Blocked", "category": "in_progress"},
        )
        self.workflow = Workflow.objects.create(
            name="PLS-530 WF",
            slug="pls-530-wf",
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
        self.leads_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name="Escalate to Leads",
            from_status="open",
            to_status="blocked",
            post_functions=[
                {"type": "assign_team", "value": "ceo-decisions"},
                {
                    "type": "create_blocker_task",
                    "team": "ceo-decisions",
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
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name="ceo-decisions",
            defaults={
                "organization_slug": "shizuha",
                "tiers": [[self.ceo_decision_owner.email]],
                "is_active": True,
            },
        )
        for team, owner in (
            ("devops", self.devops_owner),
            ("merge", self.merge_owner),
        ):
            TeamRoster.objects.update_or_create(
                organization_id=1,
                team_name=team,
                defaults={
                    "organization_slug": "shizuha",
                    "tiers": [[owner.email]],
                    "is_active": True,
                },
            )
        WorkflowService._invalidate_team_roster_caches()

    def _parent(self, suffix):
        with patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            return Item.objects.create(
                project=self.project,
                item_key=f"P530-{suffix}",
                mode="task",
                title="Operator approval dependency",
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

    def _evidence(self, item):
        return {
            "schema": "shizuha.pulse.escalation-evidence.v1",
            "request_id": str(uuid.uuid4()),
            "expected_generation": 0,
            "dependency": {
                "evidence_class": "operator_decision",
                "requested_kind": "decision",
                "requested_subject": "Approve the Origin organization policy exception",
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
        if canonical == "ceo-decisions":
            return self.ceo_decision_owner.id, self.ceo_decision_owner.email
        if canonical == "devops":
            return self.devops_owner.id, self.devops_owner.email
        if canonical == "merge":
            return self.merge_owner.id, self.merge_owner.email
        return self.engineer.id, self.engineer.email

    def _enable_ceo_decisions_lane(self):
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name="ceo-decisions",
            defaults={
                "organization_slug": "shizuha",
                "tiers": [[self.ceo_decision_owner.email]],
                "is_active": True,
            },
        )
        WorkflowService._invalidate_team_roster_caches()

    def _execute_unpatched(self, item, transition=None):
        transition = transition or self.transition
        return WorkflowService.execute_transition(
            item,
            "blocked",
            user=self.actor,
            screen_data={
                "comment": "operator decision required",
                "escalation_evidence": self._evidence(item),
            },
            transition_id=transition.id,
        )

    def _execute(self, item, transition=None):
        with patch.object(
            WorkflowService, "_resolve_team_assignee", side_effect=self._resolve_assignee,
        ), patch.object(
            WorkflowService, "_seat_assignment_allowed", return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            return self._execute_unpatched(item, transition)

    def _blockers(self, item):
        return [
            link.source_item
            for link in ItemLink.objects.filter(
                target_item=item, link_type="blocks",
            ).select_related("source_item")
        ]

    def _legacy_operator_blocker(
        self,
        parent,
        suffix,
        *,
        assignment_group="admin-ops",
        owner=None,
        title="Approve the Origin organization policy exception",
        description="Approve the Origin organization policy exception.",
    ):
        # An operator ask minted BEFORE PLS-530: active, linked (blocks), admin-ops,
        # carrying NO escalation_blocker_dependency_key metadata and NO registry row.
        with patch.object(
            WorkflowService, "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            owner = owner or (
                self.ceo_decision_owner
                if WorkflowService._canonical_team_name(assignment_group)
                == "ceo-decisions"
                else self.admin_owner
            )
            blocker = Item.objects.create(
                project=self.project, item_key=f"P530-{suffix}", mode="task",
                title=title, description=description,
                workflow_name="simple", status="open", workflow_status="open",
                assignment_group=assignment_group,
                assignee_id=owner.id, assignee_email=owner.email,
                created_by_id=self.actor.id, created_by_email=self.actor.email,
            )
        link = ItemLink(
            source_item=blocker, target_item=parent, link_type="blocks",
            created_by_id=self.actor.id, created_by_email=self.actor.email,
        )
        link._skip_auto_block = True
        link.save()
        return blocker

    def _engineering_leaf(self, parent, suffix):
        return self._team_leaf(parent, suffix, team="engineering", owner=self.engineer)

    def _team_leaf(self, parent, suffix, *, team, owner, title=None):
        with patch.object(
            WorkflowService, "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            leaf = Item.objects.create(
                project=self.project,
                item_key=f"P530-{suffix}",
                mode="task",
                title=title or f"{team}-owned dependency",
                workflow_name=self.workflow.slug,
                status="open",
                workflow_status="open",
                assignment_group=team,
                assignee_id=owner.id,
                assignee_email=owner.email,
                created_by_id=self.actor.id,
                created_by_email=self.actor.email,
            )
        link = ItemLink(
            source_item=leaf,
            target_item=parent,
            link_type="blocks",
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        link._skip_auto_block = True
        link.save()
        return leaf

    def test_legacy_preexisting_operator_blocker_is_reused_not_duplicated(self):
        # PLS-530 P1: a parent already carrying an active linked operator ask that
        # predates the registry/metadata must REUSE it, not mint the BKSG-42
        # duplicate. The legacy blocker is claimed (registry + dependency key
        # backfilled) and the blocker count does not increase.
        parent = self._parent("LEGACY-10")
        legacy = self._legacy_operator_blocker(parent, "LEGACY-OP-90")
        self.assertFalse((legacy.metadata or {}).get("escalation_blocker_dependency_key"))
        self.assertEqual(EscalationBlockerDependency.objects.count(), 0)
        item_count_before = Item.objects.count()

        self._execute(parent)

        self.assertEqual(self._blockers(parent), [legacy])       # reused, still the only ask
        self.assertEqual(Item.objects.count(), item_count_before)  # no duplicate minted
        legacy.refresh_from_db()
        self.assertTrue((legacy.metadata or {}).get("escalation_blocker_dependency_key"))
        self.assertEqual(EscalationBlockerDependency.objects.count(), 1)  # claimed into registry
        self.assertEqual(
            list(EscalationRoutingDisposition.objects.values_list("blocker_result", flat=True)),
            ["reuse"],
        )

    def test_unrelated_same_team_legacy_blocker_is_not_claimed(self):
        """A same-team link without a matching ask must not become canonical."""
        parent = self._parent("LEGACY-UNRELATED-10")
        unrelated = self._legacy_operator_blocker(
            parent,
            "LEGACY-UNRELATED-90",
            title="Authorize the unrelated finance vendor contract",
            description="Separate procurement approval for a different supplier.",
        )

        result = self._execute(parent)

        blockers = self._blockers(result)
        self.assertIn(unrelated, blockers)
        self.assertEqual(len(blockers), 2)
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(disposition.blocker_result, "mint")
        self.assertNotEqual(disposition.child_item_id, unrelated.pk)
        unrelated.refresh_from_db()
        self.assertFalse(
            (unrelated.metadata or {}).get("escalation_blocker_dependency_key"),
        )
        dependency = EscalationBlockerDependency.objects.get()
        self.assertEqual(dependency.blocker_id, disposition.child_item_id)

    def test_rollback_hold_keeps_parent_with_active_team_leaf(self):
        """The real execute rollback path must honor suppress_team.parent_group."""
        parent = self._parent("ROLLBACK-PARENT")
        Item.objects.filter(pk=parent.pk).update(
            assignment_group=None,
            assignee_id=None,
            assignee_email=None,
        )
        parent.refresh_from_db()
        leaf = self._engineering_leaf(parent, "ROLLBACK-LEAF")
        original_apply = escalation_routing._apply_parent_plan
        calls = {"count": 0}

        def fail_first_apply(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise escalation_routing._PlanFailure("injected_rollback")
            return original_apply(*args, **kwargs)

        with patch.object(
            escalation_routing,
            "_apply_parent_plan",
            side_effect=fail_first_apply,
        ):
            result = self._execute(parent)

        result.refresh_from_db()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.assignment_group, "engineering")
        self.assertEqual(result.assignee_email, self.engineer.email)
        self.assertEqual(self._blockers(result), [leaf])
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(disposition.blocker_result, "suppress_team")
        self.assertEqual(disposition.reason_code, "injected_rollback")

    def test_forced_hold_keeps_parent_with_active_team_leaf(self):
        """The fail-loud forced caller must honor suppress_team.parent_group."""
        parent = self._parent("FORCED-PARENT")
        Item.objects.filter(pk=parent.pk).update(
            assignment_group=None,
            assignee_id=None,
            assignee_email=None,
        )
        parent.refresh_from_db()
        leaf = self._engineering_leaf(parent, "FORCED-LEAF")

        with patch.object(
            WorkflowService, "_resolve_team_assignee", side_effect=self._resolve_assignee,
        ), patch.object(
            WorkflowService, "_seat_assignment_allowed", return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            result = escalation_routing.EscalationRoutingService._commit_unexpected_hold(
                parent.pk,
                self.transition,
                user=self.actor,
                screen_data={},
                reason_code="unexpected_error",
            )

        result.refresh_from_db()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.assignment_group, "engineering")
        self.assertEqual(result.assignee_email, self.engineer.email)
        self.assertEqual(self._blockers(result), [leaf])
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(disposition.blocker_result, "suppress_team")

    def test_legacy_claim_lets_second_same_ask_parent_dedupe_via_registry(self):
        # PLS-530 P1 (org-wide preexisting-ask case): after the first parent claims
        # the legacy blocker into the registry, a second parent with the same
        # structured ask reuses it via the registry pointer — one operator ask total.
        first = self._parent("LEGACY-ORGA")
        legacy = self._legacy_operator_blocker(first, "LEGACY-OP-91")
        self._execute(first)
        second = self._parent("LEGACY-ORGB")
        self._execute(second)

        self.assertEqual(self._blockers(second), [legacy])
        self.assertEqual(EscalationBlockerDependency.objects.count(), 1)

    def test_forced_hold_without_envelope_dedupes_repeated_callers(self):
        # PLS-530 P1: the fail-loud/forced-hold paths call _prepare_required_blocker
        # with prepared=None. The namespaced per-item fallback dependency key must
        # dedupe repeated forced holds on the same item to ONE operator child
        # instead of returning "" and bypassing the mint/reuse gate.
        parent = self._parent("FORCED-10")
        plan = _Plan(
            classification="mixed_or_unknown_escalation_hold",
            outcome="held",
            reason_code="forced_hold_recovery",
            target_status="blocked",
            target_group="admin-ops",
        )
        pre = {
            "status": "open", "assignment_group": "engineering",
            "assignee_id": self.engineer.id, "assignee_email": self.engineer.email,
        }

        def _call():
            with patch.object(
                WorkflowService, "_resolve_team_assignee", side_effect=self._resolve_assignee,
            ), patch.object(
                WorkflowService, "_seat_assignment_allowed", return_value=True,
            ), patch.object(
                WorkflowService, "assignment_org_mismatch",
                return_value=(False, "shizuha", "shizuha"),
            ), transaction.atomic():
                return _prepare_required_blocker(
                    parent, plan, self.transition, pre=pre, user=self.actor,
                    screen_data={}, prepared=None,
                )

        first = _call()
        second = _call()

        self.assertNotEqual(first.dependency_key, "")           # gate NOT bypassed
        self.assertEqual(first.dependency_key, second.dependency_key)  # stable per-item
        self.assertEqual(first.result, "mint")
        self.assertEqual(second.result, "reuse")
        operator_children = [
            b for b in self._blockers(parent)
            if WorkflowService._canonical_team_name(b.assignment_group) == "admin-ops"
        ]
        self.assertEqual(len(operator_children), 1)             # exactly one child

    def test_distinct_parents_rollback_hold_reuses_typed_dependency(self):
        """Real rollback callers retain the cross-parent dependency identity."""
        parents = [self._parent("rollback-shared-1"), self._parent("rollback-shared-2")]
        Item.objects.filter(pk__in=[parent.pk for parent in parents]).update(
            assignment_group=None,
            assignee_id=None,
            assignee_email=None,
        )
        for parent in parents:
            parent.refresh_from_db()
        original_apply = escalation_routing._apply_parent_plan
        failed_parents = set()
        parent_ids = {parent.pk for parent in parents}

        def fail_each_initial_plan(item, plan, **kwargs):
            if (
                item.pk in parent_ids
                and item.pk not in failed_parents
                and plan.classification != "mixed_or_unknown_escalation_hold"
            ):
                failed_parents.add(item.pk)
                raise escalation_routing._PlanFailure("injected_shared_rollback")
            return original_apply(item, plan, **kwargs)

        with patch.object(
            escalation_routing,
            "_apply_parent_plan",
            side_effect=fail_each_initial_plan,
        ):
            for parent in parents:
                self._execute(parent)

        blockers = [self._blockers(parent)[0] for parent in parents]
        self.assertEqual(len({blocker.pk for blocker in blockers}), 1)
        self.assertEqual(EscalationBlockerDependency.objects.count(), 1)
        self.assertEqual(
            list(EscalationRoutingDisposition.objects.order_by("created_at").values_list(
                "blocker_result", flat=True,
            )),
            ["mint", "reuse"],
        )
        self.assertEqual(
            set(EscalationRoutingDisposition.objects.values_list("reason_code", flat=True)),
            {"injected_shared_rollback"},
        )

    def test_bksg_second_parent_reuses_canonical_operator_ask_without_third_layer(self):
        first = self._parent("BKSG-20")
        second = self._parent("BKSG-40")

        self._execute(first)
        first_blocker = self._blockers(first)[0]
        self._execute(second)

        self.assertEqual(self._blockers(second), [first_blocker])
        self.assertEqual(
            Item.objects.filter(metadata__escalation_blocker_dependency_key__isnull=False).count(),
            1,
        )
        self.assertEqual(EscalationBlockerDependency.objects.count(), 1)
        self.assertEqual(
            list(EscalationRoutingDisposition.objects.order_by("created_at").values_list(
                "blocker_result", flat=True,
            )),
            ["mint", "reuse"],
        )

    def test_second_parent_repairs_unowned_canonical_operator_ask_before_reuse(self):
        """The top-level second transition repairs a cleared canonical owner."""
        first = self._parent("OWNER-REPAIR-20")
        second = self._parent("OWNER-REPAIR-40")

        self._execute(first)
        canonical = self._blockers(first)[0]
        canonical_group = canonical.assignment_group
        canonical_owner_id = canonical.assignee_id
        canonical_owner_email = canonical.assignee_email
        Item.objects.filter(pk=canonical.pk).update(
            assignee_id=None,
            assignee_email=None,
        )
        canonical.refresh_from_db()
        self.assertEqual(canonical.assignment_group, canonical_group)
        self.assertIsNone(canonical.assignee_id)
        dependency = EscalationBlockerDependency.objects.get()
        self.assertEqual(dependency.blocker_id, canonical.pk)

        result = self._execute(second)

        canonical.refresh_from_db()
        result.refresh_from_db()
        self.assertEqual(canonical.assignee_id, canonical_owner_id)
        self.assertEqual(canonical.assignee_email, canonical_owner_email)
        self.assertEqual(self._blockers(result), [canonical])
        self.assertEqual(result.status, "blocked")
        self.assertEqual(
            list(EscalationRoutingDisposition.objects.order_by("created_at").values_list(
                "blocker_result", flat=True,
            )),
            ["mint", "reuse"],
        )

    def test_escalate_to_leads_reuses_one_ceo_decision_child(self):
        """Repeated top-level Leads escalations must use the operator dedupe gate."""
        self._enable_ceo_decisions_lane()
        first = self._parent("LEADS-20")
        second = self._parent("LEADS-40")

        self._execute(first, self.leads_transition)
        first_blocker = self._blockers(first)[0]
        self._execute(second, self.leads_transition)

        self.assertEqual(first_blocker.assignment_group, "ceo-decisions")
        self.assertEqual(self._blockers(second), [first_blocker])
        self.assertEqual(EscalationBlockerDependency.objects.count(), 1)
        self.assertEqual(
            list(EscalationRoutingDisposition.objects.order_by("created_at").values_list(
                "blocker_result", flat=True,
            )),
            ["mint", "reuse"],
        )

    def test_escalate_to_human_ops_stays_on_the_physical_lane(self):
        """A physical escalation follows the transition, not the CEO lane."""
        human = get_user_model().objects.create_user(
            username="pls530-human-ops",
            email="pls530-human-ops@example.com",
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name="human-ops",
            defaults={
                "organization_slug": "shizuha",
                "tiers": [[human.email]],
                "is_active": True,
            },
        )
        WorkflowService._invalidate_team_roster_caches()
        transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name="Escalate to Human Ops",
            from_status="open",
            to_status="blocked",
            post_functions=[
                {"type": "assign_team", "value": "human-ops"},
                {
                    "type": "create_blocker_task",
                    "team": "human-ops",
                    "workflow": "simple",
                    "priority": "urgent",
                },
            ],
        )
        parent = self._parent("HUMAN-20")

        def resolve(team, **_kwargs):
            if WorkflowService._canonical_team_name(team) == "human-ops":
                return human.id, human.email
            return self._resolve_assignee(team)

        with patch.object(
            WorkflowService, "_resolve_team_assignee", side_effect=resolve,
        ), patch.object(
            WorkflowService, "_seat_assignment_allowed", return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            result = self._execute_unpatched(parent, transition)

        result.refresh_from_db()
        blocker = self._blockers(result)[0]
        self.assertEqual(result.assignment_group, "human-ops")
        self.assertEqual(blocker.assignment_group, "human-ops")
        self.assertEqual(blocker.assignee_email, human.email)
        self.assertEqual(
            EscalationBlockerDependency.objects.get().target_team,
            "human-ops",
        )

    def test_existing_ceo_decision_blocker_is_operator_reuse_not_team_suppression(self):
        """A linked CEO decision ask is operator-class, never a team-owned leaf."""
        self._enable_ceo_decisions_lane()
        parent = self._parent("LEADS-LEGACY-10")
        legacy = self._legacy_operator_blocker(
            parent,
            "LEADS-LEGACY-90",
            assignment_group="ceo-decisions",
            owner=self.ceo_decision_owner,
        )

        self._execute(parent, self.leads_transition)

        self.assertEqual(self._blockers(parent), [legacy])
        disposition = EscalationRoutingDisposition.objects.get(item=parent)
        self.assertEqual(disposition.blocker_result, "reuse")
        self.assertNotEqual(disposition.blocker_result, "suppress_team")

    def test_wrong_team_registry_fails_closed_without_success_mutations(self):
        """PLS-905: registry identity/team disagreement is a distinct HOLD."""
        first = self._parent("REGISTRY-MISMATCH-FIRST")
        self._execute(first)
        dependency = EscalationBlockerDependency.objects.get()
        canonical_id = dependency.blocker_id
        # Raise to Admin Ops registers admin-ops. A later registry rewrite onto
        # ceo-decisions must fail closed and must not rehome the child.
        dependency.target_team = "ceo-decisions"
        dependency.save(update_fields=["target_team", "updated_at"])
        second = self._parent("REGISTRY-MISMATCH-SECOND")
        Item.objects.filter(pk=second.pk).update(
            assignment_group="ceo-decisions",
            assignee_id=self.ceo_decision_owner.id,
            assignee_email=self.ceo_decision_owner.email,
        )
        second.refresh_from_db()

        result = self._execute(second)

        result.refresh_from_db()
        dependency.refresh_from_db()
        canonical = Item.objects.get(pk=canonical_id)
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(disposition.classification, "mixed_or_unknown_escalation_hold")
        self.assertEqual(disposition.outcome, "held")
        self.assertEqual(
            disposition.reason_code,
            "operator_dependency_registry_team_mismatch",
        )
        self.assertEqual(
            result._escalation_receipt_payload["reason_code"],
            "operator_dependency_registry_team_mismatch",
        )
        self.assertEqual(result._escalation_receipt_payload["outcome"], "held")
        self.assertEqual(dependency.target_team, "ceo-decisions")
        self.assertEqual(dependency.blocker_id, canonical_id)
        self.assertEqual(canonical.assignment_group, "admin-ops")
        self.assertNotEqual(disposition.child_item_id, canonical_id)
        self.assertNotEqual(disposition.child_item.status, "waiting_external")

    def test_wrong_team_registry_child_fails_closed_without_rehoming_child(self):
        """PLS-905: a wrong-owner canonical child is never repaired as success."""
        first = self._parent("CHILD-MISMATCH-FIRST")
        self._execute(first)
        dependency = EscalationBlockerDependency.objects.get()
        canonical = dependency.blocker
        Item.objects.filter(pk=canonical.pk).update(
            assignment_group="ceo-decisions",
            assignee_id=self.ceo_decision_owner.id,
            assignee_email=self.ceo_decision_owner.email,
        )
        second = self._parent("CHILD-MISMATCH-SECOND")
        Item.objects.filter(pk=second.pk).update(
            assignment_group="ceo-decisions",
            assignee_id=self.ceo_decision_owner.id,
            assignee_email=self.ceo_decision_owner.email,
        )
        second.refresh_from_db()

        result = self._execute(second)

        result.refresh_from_db()
        dependency.refresh_from_db()
        canonical.refresh_from_db()
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(disposition.classification, "mixed_or_unknown_escalation_hold")
        self.assertEqual(disposition.outcome, "held")
        self.assertEqual(
            disposition.reason_code,
            "operator_dependency_child_team_mismatch",
        )
        self.assertEqual(
            result._escalation_receipt_payload["reason_code"],
            "operator_dependency_child_team_mismatch",
        )
        self.assertEqual(dependency.target_team, "admin-ops")
        self.assertEqual(dependency.blocker_id, canonical.pk)
        self.assertEqual(canonical.assignment_group, "ceo-decisions")
        self.assertEqual(canonical.assignee_email, self.ceo_decision_owner.email)
        self.assertNotEqual(disposition.child_item_id, canonical.pk)
        self.assertNotEqual(disposition.child_item.status, "waiting_external")

    def test_unrelated_devops_blocker_does_not_suppress_operator_dependency(self):
        """PLS-905: a deployment carrier is not the typed operator ask."""
        parent = self._parent("NTS-52")
        leaf = self._team_leaf(
            parent,
            "ORIG-98",
            team="devops",
            owner=self.devops_owner,
            title="Record correlated production DeploymentEvent",
        )

        result = self._execute(parent)

        result.refresh_from_db()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.assignment_group, "admin-ops")
        blockers = self._blockers(result)
        self.assertIn(leaf, blockers)
        self.assertEqual(len(blockers), 2)
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(disposition.blocker_result, "mint")
        self.assertNotEqual(disposition.child_item_id, leaf.pk)
        dependency = EscalationBlockerDependency.objects.get()
        self.assertEqual(dependency.target_team, "admin-ops")
        self.assertEqual(dependency.blocker_id, disposition.child_item_id)
        operator_child = dependency.blocker
        self.assertEqual(operator_child.assignment_group, "admin-ops")
        self.assertEqual(operator_child.assignee_email, self.admin_owner.email)
        leaf.refresh_from_db()
        self.assertEqual(leaf.assignment_group, "devops")
        self.assertFalse(
            (leaf.metadata or {}).get("escalation_blocker_dependency_key"),
        )

    def test_schema_repair_blocker_does_not_suppress_operator_dependency(self):
        """PLS-905: schema repair and holder dependency remain distinct."""
        parent = self._parent("GROUP-ONLY-TEAM-PARENT")
        leaf = self._team_leaf(
            parent,
            "SCHEMA-REPAIR-LEAF",
            team="merge",
            owner=self.merge_owner,
            title="[SCHEMA REPAIR: missing_evidence_schema] repair typed evidence",
        )

        result = self._execute(parent)

        result.refresh_from_db()
        leaf.refresh_from_db()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.assignment_group, "admin-ops")
        self.assertEqual(result.assignee_email, self.admin_owner.email)
        self.assertEqual(leaf.assignment_group, "merge")
        self.assertEqual(leaf.assignee_id, self.merge_owner.id)
        blockers = self._blockers(result)
        self.assertIn(leaf, blockers)
        self.assertEqual(len(blockers), 2)
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(disposition.blocker_result, "mint")
        self.assertNotEqual(disposition.child_item_id, leaf.pk)

    def test_unrelated_blocker_is_preserved_without_owner_churn(self):
        """Exact-key routing never selects or reassigns an unrelated blocker."""
        parent = self._parent("OWNED-TEAM-PARENT")
        leaf = self._engineering_leaf(parent, "OWNED-TEAM-LEAF")
        alternate = get_user_model().objects.create_user(
            username="pls530-alternate-engineer",
            email="pls530-alternate-engineer@example.com",
        )
        TeamRoster.objects.filter(
            organization_id=1,
            team_name="engineering",
        ).update(tiers=[[self.engineer.email, alternate.email]])
        WorkflowService._invalidate_team_roster_caches()

        def resolve_with_alternate_engineer(team, **_kwargs):
            if WorkflowService._canonical_team_name(team) == "engineering":
                return alternate.id, alternate.email
            return self._resolve_assignee(team)

        with patch.object(
            WorkflowService,
            "_resolve_team_assignee",
            side_effect=resolve_with_alternate_engineer,
        ), patch.object(
            WorkflowService,
            "_seat_assignment_allowed",
            return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            result = self._execute_unpatched(parent)

        result.refresh_from_db()
        leaf.refresh_from_db()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.assignment_group, "admin-ops")
        self.assertEqual(result.assignee_id, self.admin_owner.id)
        self.assertEqual(leaf.assignment_group, "engineering")
        self.assertEqual(leaf.assignee_id, self.engineer.id)
        self.assertEqual(leaf.assignee_email, self.engineer.email)
        blockers = self._blockers(result)
        self.assertIn(leaf, blockers)
        self.assertEqual(len(blockers), 2)
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(disposition.blocker_result, "mint")

    def test_empty_operator_election_mints_exact_group_owned_dependency(self):
        """PLS-905/860: unrelated leaf cannot replace a group-owned Admin Ops ask."""
        parent = self._parent("UNRESOLVED-TEAM-PARENT")
        leaf = self._engineering_leaf(parent, "UNRESOLVED-TEAM-LEAF")
        Item.objects.filter(pk=leaf.pk).update(
            assignee_id=None,
            assignee_email=None,
        )
        item_count_before = Item.objects.count()

        with patch.object(
            WorkflowService,
            "_resolve_team_assignee",
            return_value=(None, None),
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            result = self._execute_unpatched(parent)

        result.refresh_from_db()
        leaf.refresh_from_db()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.assignment_group, "admin-ops")
        self.assertIsNone(result.assignee_id)
        self.assertEqual(Item.objects.count(), item_count_before + 1)
        blockers = self._blockers(result)
        self.assertIn(leaf, blockers)
        self.assertEqual(len(blockers), 2)
        self.assertIsNone(leaf.assignee_id)
        self.assertFalse(leaf.assignee_email)
        self.assertEqual(leaf.assignment_group, "engineering")
        self.assertFalse(
            (leaf.metadata or {}).get("escalation_group_owned_wait"),
        )
        dependency = EscalationBlockerDependency.objects.get()
        operator_child = dependency.blocker
        self.assertEqual(operator_child.assignment_group, "admin-ops")
        self.assertIsNone(operator_child.assignee_id)
        meta = operator_child.metadata or {}
        self.assertTrue(meta.get("escalation_group_owned_wait"))
        self.assertEqual(meta.get("escalation_group_owned_cause"), "election_empty")
        self.assertEqual(
            EscalationRoutingDisposition.objects.filter(item=result).count(),
            1,
        )
        self.assertEqual(
            EscalationRoutingDisposition.objects.get(item=result).blocker_result,
            "mint",
        )

    def test_unregistered_team_leaf_still_rejects_without_recursive_hold(self):
        """Unregistered exact-org team remains a hard error (not group-owned)."""
        parent = self._parent("UNREGISTERED-TEAM-PARENT")
        leaf = self._engineering_leaf(parent, "UNREGISTERED-TEAM-LEAF")
        Item.objects.filter(pk=leaf.pk).update(
            assignee_id=None,
            assignee_email=None,
        )
        item_count_before = Item.objects.count()

        with patch.object(
            WorkflowService,
            "_resolve_team_assignee",
            return_value=(None, None),
        ), patch.object(
            WorkflowService,
            "_team_registered_for_item",
            return_value=False,
        ), self.assertRaises(WorkflowValidationError) as raised:
            self._execute_unpatched(parent)

        self.assertIn(
            raised.exception.code,
            {"BLOCKER_ASSIGNEE_UNAVAILABLE", "BLOCKER_TEAM_UNREGISTERED"},
        )
        parent.refresh_from_db()
        leaf.refresh_from_db()
        self.assertEqual(parent.status, "open")
        self.assertEqual(Item.objects.count(), item_count_before)
        self.assertEqual(self._blockers(parent), [leaf])
        self.assertFalse(EscalationRoutingDisposition.objects.filter(item=parent).exists())

    def test_metrics_report_fixed_mint_reuse_suppress_labels(self):
        first = self._parent("metrics-1")
        second = self._parent("metrics-2")
        self._execute(first)
        self._execute(second)

        body = render_escalation_blocker_metrics()

        self.assertIn('pulse_escalation_mint_total{result="mint"} 1', body)
        self.assertIn('pulse_escalation_mint_total{result="reuse"} 1', body)
        self.assertIn('pulse_escalation_mint_total{result="suppress_team"} 0', body)

    @skipUnless(connection.vendor == "postgresql", "row-lock concurrency requires PostgreSQL")
    def test_concurrent_parents_converge_on_single_operator_mint(self):
        parents = [self._parent("concurrent-1"), self._parent("concurrent-2")]
        barrier = threading.Barrier(2)
        errors = []

        def worker(parent_id):
            close_old_connections()
            try:
                parent = Item.objects.get(pk=parent_id)
                barrier.wait(timeout=5)
                self._execute_unpatched(parent)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                close_old_connections()

        with patch.object(
            WorkflowService,
            "_resolve_team_assignee",
            side_effect=self._resolve_assignee,
        ), patch.object(
            WorkflowService, "_seat_assignment_allowed", return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            threads = [
                threading.Thread(target=worker, args=(parent.pk,)) for parent in parents
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        blockers = [self._blockers(parent)[0].pk for parent in parents]
        self.assertEqual(len(set(blockers)), 1)
        self.assertEqual(EscalationBlockerDependency.objects.count(), 1)
        self.assertEqual(
            sorted(EscalationRoutingDisposition.objects.values_list(
                "blocker_result", flat=True,
            )),
            ["mint", "reuse"],
        )

    @skipUnless(connection.vendor == "postgresql", "row-lock concurrency requires PostgreSQL")
    def test_concurrent_forced_holds_reuse_shared_typed_dependency(self):
        """Real unexpected-hold callers preserve one typed key across parents."""
        parents = [self._parent("forced-shared-1"), self._parent("forced-shared-2")]
        Item.objects.filter(pk__in=[parent.pk for parent in parents]).update(
            assignment_group=None,
            assignee_id=None,
            assignee_email=None,
        )
        for parent in parents:
            parent.refresh_from_db()
        barrier = threading.Barrier(2)
        errors = []

        def worker(parent_id):
            close_old_connections()
            try:
                parent = Item.objects.get(pk=parent_id)
                barrier.wait(timeout=5)
                escalation_routing.EscalationRoutingService._commit_unexpected_hold(
                    parent.pk,
                    self.transition,
                    user=self.actor,
                    screen_data={
                        "comment": "operator decision required",
                        "escalation_evidence": self._evidence(parent),
                    },
                    reason_code="unexpected_shared_failure",
                )
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                close_old_connections()

        with patch.object(
            WorkflowService,
            "_resolve_team_assignee",
            side_effect=self._resolve_assignee,
        ), patch.object(
            WorkflowService, "_seat_assignment_allowed", return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            threads = [
                threading.Thread(target=worker, args=(parent.pk,)) for parent in parents
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        blockers = [self._blockers(parent)[0] for parent in parents]
        self.assertEqual(len({blocker.pk for blocker in blockers}), 1)
        self.assertEqual(EscalationBlockerDependency.objects.count(), 1)
        self.assertEqual(
            sorted(EscalationRoutingDisposition.objects.values_list(
                "blocker_result", flat=True,
            )),
            ["mint", "reuse"],
        )

    @skipUnless(connection.vendor == "postgresql", "row-lock concurrency requires PostgreSQL")
    def test_concurrent_escalate_to_leads_calls_converge_on_one_ceo_decision_child(self):
        """Concurrent real Leads transitions mint once, then reuse the same child."""
        self._enable_ceo_decisions_lane()
        parents = [self._parent("leads-concurrent-1"), self._parent("leads-concurrent-2")]
        barrier = threading.Barrier(2)
        errors = []

        def worker(parent_id):
            close_old_connections()
            try:
                parent = Item.objects.get(pk=parent_id)
                barrier.wait(timeout=5)
                self._execute_unpatched(parent, self.leads_transition)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                close_old_connections()

        with patch.object(
            WorkflowService,
            "_resolve_team_assignee",
            side_effect=self._resolve_assignee,
        ), patch.object(
            WorkflowService, "_seat_assignment_allowed", return_value=True,
        ), patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            threads = [
                threading.Thread(target=worker, args=(parent.pk,)) for parent in parents
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        blockers = [self._blockers(parent)[0] for parent in parents]
        self.assertEqual(len({blocker.pk for blocker in blockers}), 1)
        self.assertEqual(blockers[0].assignment_group, "ceo-decisions")
        self.assertEqual(EscalationBlockerDependency.objects.count(), 1)
        self.assertEqual(
            sorted(EscalationRoutingDisposition.objects.values_list(
                "blocker_result", flat=True,
            )),
            ["mint", "reuse"],
        )
