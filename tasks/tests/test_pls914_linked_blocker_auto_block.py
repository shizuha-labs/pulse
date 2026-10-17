"""PLS-914: linked-blocker auto-block must never select an operator escalation
transition or fabricate a schema-repair child.

Regression: on PLAT-4494 / PLAT-5540, creating an ordinary ``blocks`` link
moved the parent to ``blocked`` via the first target-compatible transition —
which was ``Escalate to Leads`` — routing through the typed-evidence service
and minting a ``[SCHEMA REPAIR: missing_evidence_schema]`` child even though no
operator escalation was requested.

The auto-block path must select ONLY a transition explicitly classified for
linked blockers (targets ``blocked`` and is not an operator/human escalation
transition), never by target status alone.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from tasks.models import (
    Comment,
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


class Pls914LinkedBlockerAutoBlockTests(TransactionTestCase):
    """Exercise the real link-creation -> auto-block transaction sequence."""

    def setUp(self):
        user_model = get_user_model()
        self.actor = user_model.objects.create_user(
            username="pls914-actor",
            email="pls914-actor@example.com",
            is_staff=True,
        )
        self.project = Project.objects.create(name="PLS-914", key="P914", org_id=1)

        open_status, _ = Status.objects.get_or_create(
            slug="open", defaults={"name": "Open", "category": "todo"},
        )
        in_progress_status, _ = Status.objects.get_or_create(
            slug="in_progress", defaults={"name": "In Progress", "category": "in_progress"},
        )
        blocked_status, _ = Status.objects.get_or_create(
            slug="blocked", defaults={"name": "Blocked", "category": "in_progress"},
        )
        done_status, _ = Status.objects.get_or_create(
            slug="done", defaults={"name": "Done", "category": "done"},
        )
        self.workflow = Workflow.objects.create(
            name="PLS-914 WF",
            slug="pls-914-wf",
            initial_status=open_status,
            is_active=True,
        )
        WorkflowStatus.objects.create(workflow=self.workflow, status=open_status, position=0)
        WorkflowStatus.objects.create(workflow=self.workflow, status=in_progress_status, position=1)
        WorkflowStatus.objects.create(workflow=self.workflow, status=blocked_status, position=2)
        WorkflowStatus.objects.create(workflow=self.workflow, status=done_status, position=3)

        # Three transitions sharing target `blocked` — the exact shape that
        # reproduced the bug: the escalation transitions sort FIRST by position
        # so a naive `qs.first()` fallback picks Escalate to Leads.
        self.leads = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name="Escalate to Leads",
            from_status="open",
            to_status="blocked",
            position=1,
            post_functions=[{"type": "assign_team", "value": "ceo-decisions"}],
        )
        self.admin_ops = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name="Raise to Admin Ops",
            from_status="open",
            to_status="blocked",
            position=2,
            post_functions=[{"type": "assign_team", "value": "admin-ops"}],
        )
        self.linked = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name="Blocked by Linked Issue",
            from_status="open",
            to_status="blocked",
            position=3,
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

    def _item(self, suffix, *, status="open", group="engineering"):
        with patch.object(
            WorkflowService,
            "assignment_org_mismatch",
            return_value=(False, "shizuha", "shizuha"),
        ):
            return Item.objects.create(
                project=self.project,
                item_key=f"P914-{suffix}",
                mode="task",
                title="PLS-914 parent",
                description="ordinary dependency",
                workflow_name=self.workflow.slug,
                status=status,
                workflow_status=status,
                assignment_group=group,
                assignee_id=self.actor.id,
                assignee_email=self.actor.email,
                created_by_id=self.actor.id,
                created_by_email=self.actor.email,
            )

    def test_blocks_link_selects_linked_blocker_transition_not_escalation(self):
        blocker = self._item("BLOCKER", status="in_progress")
        dependent = self._item("DEPENDENT")

        link = ItemLink.objects.create(
            source_item=blocker,
            target_item=dependent,
            link_type="blocks",
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )

        dependent.refresh_from_db()
        self.assertEqual(dependent.status, "blocked")
        # The parent must carry exactly ONE blocker: the real engineering child.
        links = ItemLink.objects.filter(target_item=dependent, link_type="blocks")
        self.assertEqual(links.count(), 1)
        self.assertEqual(links.get().source_item_id, blocker.pk)

        # No escalation/schema-repair child may exist.
        self.assertFalse(
            Item.objects.filter(
                title__startswith="[SCHEMA REPAIR:",
            ).exists()
        )
        self.assertFalse(
            Item.objects.filter(
                metadata__escalation_schema_repair__isnull=False,
            ).exists()
        )
        # The selected transition was the linked-blocker one — no escalation
        # transition was executed.
        self.assertFalse(
            Comment.objects.filter(
                item=dependent,
                content__icontains="Escalate to Leads",
            ).exists()
        )
        self.assertFalse(
            Comment.objects.filter(
                item=dependent,
                content__icontains="Raise to Admin Ops",
            ).exists()
        )

    def test_find_linked_blocker_transition_never_returns_escalation(self):
        dependent = self._item("DEP2")
        transition = WorkflowService._find_linked_blocker_transition(dependent)
        self.assertIsNotNone(transition)
        self.assertEqual(transition.id, self.linked.id)
        self.assertEqual(transition.name, "Blocked by Linked Issue")

    def test_find_linked_blocker_transition_returns_none_when_only_escalations(self):
        # Remove the linked-blocker transition: only escalation transitions
        # target `blocked` from `open`. Auto-block must refuse (return None)
        # rather than fabricate an escalation/schema-repair child.
        self.linked.delete()
        dependent = self._item("DEP3")
        transition = WorkflowService._find_linked_blocker_transition(dependent)
        self.assertIsNone(transition)

    def test_auto_block_refuses_when_only_escalation_transitions_exist(self):
        self.linked.delete()
        blocker = self._item("BLOCKER2", status="in_progress")
        dependent = self._item("DEP4")

        link = ItemLink(
            source_item=blocker,
            target_item=dependent,
            link_type="blocks",
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        # Suppress the post-save signal so we exercise auto_block_from_link
        # directly against a persisted link row (the signal path is covered by
        # the first test).
        link._skip_auto_block = True
        link.save()

        result = WorkflowService.auto_block_from_link(link)
        self.assertFalse(result)
        dependent.refresh_from_db()
        # Status stays put (queue priority still holds the row non-runnable),
        # and NO escalation/schema-repair child is fabricated.
        self.assertEqual(dependent.status, "open")
        self.assertFalse(
            Item.objects.filter(title__startswith="[SCHEMA REPAIR:").exists()
        )
