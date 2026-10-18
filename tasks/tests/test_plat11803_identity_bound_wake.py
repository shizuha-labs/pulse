"""PLAT-11803 acceptance probe (hritik, review 6710): identity-bound wake/lease.

The acceptance contract: an urgent/high identity-bound card created on a
hibernated co-signer (sora) must (1) stay on sora — never auto-failover to a
non-co-signer (PLAT-11771: sora → hiro four minutes after creation) and never
reroute to a co-signer absent from the health map (the old ``status or
"active"`` failed OPEN on a scrape failure) — and (2) mint the wake/lease
attestation for real: a Redis payload Hive's GLM lease planner gates its
identity_bound_priority seatel on, not just a comment that claims one.
"""
import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from tasks.models import Comment, Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import (
    PLAT999_COSIGNER_WAKE_REDIS_KEY,
    _clear_plat999_cosigner_wake,
    _resolve_plat999_cosigner_failover_target,
    sweep_stale_offline_assignments,
)


def _redis_mock():
    return mock.Mock(name="redis")


def _publish_mock():
    """Patch the shared Redis client so wake publish/clear is observable."""
    m = _redis_mock()
    patcher = mock.patch.object(
        WorkflowService, "_redis_client", return_value=m,
    )
    return patcher, m


class IdentityBoundWakeAcceptanceTests(TestCase):
    def _register_roster(self, team_name="engineering", members=("sora@shizuha.com",)):
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name=team_name,
            defaults={
                "organization_slug": "shizuha",
                "tiers": [list(members)],
                "is_active": True,
            },
        )

    def _card(self, *, priority="urgent", labels=("plat-999",), key="CTX-9"):
        project = Project.objects.create(name="Cortex", key=key, org_id=1)
        card = Item.objects.create(
            mode="task",
            item_type="task.generic",
            title="Plat-999 co-sign gate",
            item_key="CTX-9",
            project=project,
            status="open",
            workflow_name="autonomous-bug",
            priority=priority,
            labels=list(labels),
            assignment_group="engineering",
            assignee_email="sora@shizuha.com",
        )
        Item.objects.filter(id=card.id).update(
            updated_at=timezone.now() - timedelta(hours=30),
        )
        return card

    def test_urgent_cosigner_card_stays_on_offline_sora_and_wake_is_minted(self):
        """Stays on assignee + wake payload published. The health map has NO
        entry for aoi (scrape failure) — the old fail-open would have rerouted
        to that nominally-active co-signer and defeated stays-on-assignee."""
        self._register_roster()
        card = self._card()
        patcher, redis = _publish_mock()
        with patcher, mock.patch(
            "tasks.views._scrape_agent_health",
            return_value={"sora": {"status": "offline"}},
        ):
            result = sweep_stale_offline_assignments.apply(
                kwargs={"hours": 24},
            ).get()

        self.assertEqual(result["alerted"], 1)
        card.refresh_from_db()
        self.assertEqual(
            card.assignee_email, "sora@shizuha.com",
            "an identity-bound card must never auto-failover to a non-co-signer",
        )
        self.assertTrue(
            Comment.objects.filter(
                item=card, content__contains="[identity-bound wake requested]",
            ).exists(),
        )
        self.assertEqual(
            card.metadata.get("identity_bound_wake_requested"),
            "sora@shizuha.com",
        )
        self.assertEqual(redis.setex.call_count, 1)
        args, kwargs_ = redis.setex.call_args
        self.assertEqual(args[0], PLAT999_COSIGNER_WAKE_REDIS_KEY)
        payload = json.loads(args[2])
        self.assertEqual(payload["assignee_email"], "sora@shizuha.com")
        self.assertEqual(payload["item_key"], "CTX-9")

    def test_urgent_cosigner_card_fails_over_within_named_set_and_clears_wake(self):
        """Failover resolves ONLY inside the co-signer set: active aoi gets the
        card, and the wake marker it minted is cleared — the attested identity
        no longer owns the ready card."""
        self._register_roster(members=("sora@shizuha.com", "aoi@shizuha.com"))
        card = self._card()
        patcher, redis = _publish_mock()
        with patcher, mock.patch(
            "tasks.views._scrape_agent_health",
            return_value={
                "sora": {"status": "offline"},
                "aoi": {"status": "active"},
            },
        ), mock.patch(
            "tasks.user_service.user_service.get_user_by_email",
            return_value={"id": 7},
        ):
            result = sweep_stale_offline_assignments.apply(
                kwargs={"hours": 24},
            ).get()

        self.assertEqual(result["alerted"], 1)
        card.refresh_from_db()
        self.assertEqual(card.assignee_email, "aoi@shizuha.com")
        self.assertTrue(
            Comment.objects.filter(
                item=card, content__contains="[stale-assignment failover]",
            ).exists(),
        )
        redis.delete.assert_called_once_with(PLAT999_COSIGNER_WAKE_REDIS_KEY)

    def test_resolve_returns_active_set_member_and_skips_absent_health(self):
        """Pure helper: missing health entry ⇒ (None, None) (no fail-open);
        active member ⇒ (uid, email)."""
        self._register_roster()
        project = Project.objects.create(name="Cortex", key="CTXH", org_id=1)
        card = Item.objects.create(
            mode="task", item_type="task.generic", title="Co-sign",
            item_key="CTXH-1", project=project, status="open",
            workflow_name="autonomous-bug", priority="urgent",
            labels=["identity-bound"], assignment_group="engineering",
            assignee_email="sora@shizuha.com",
        )
        health = {"aoi": {"status": "active"}}
        with mock.patch(
            "tasks.user_service.user_service.get_user_by_email",
            return_value={"id": 11},
        ):
            uid, email = _resolve_plat999_cosigner_failover_target(card, health)
        self.assertEqual((uid, email), (11, "aoi@shizuha.com"))

        uid, email = _resolve_plat999_cosigner_failover_target(card, {})
        self.assertEqual(
            (uid, email), (None, None),
            "a member absent from the health map is not a failover candidate",
        )

    def test_clear_wake_is_best_effort(self):
        """Redis trouble must never fail the sweep's clear path."""
        patcher, redis = _publish_mock()
        redis.delete.side_effect = ConnectionError("redis down")
        with patcher:
            self.assertIsNone(_clear_plat999_cosigner_wake())
