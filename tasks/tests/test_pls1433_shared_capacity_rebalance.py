"""PLS-1433 regression: the rebalance sweep must agree with the PLS-509 cap.

Two loop shapes observed 2026-10-08 (HIVE-2860 lineage; prior specimens
PLAT-5339 / PLS-1416 / PLS-1415):

1. The sweep selected pull targets with legacy process-wide counters while the
   reservation/verification path enforces per-roster assigned-ready caps — a
   roster-full seat was pulled and immediately returned, a pull/return storm
   on urgent carriers.
2. WIP recovery shed deliberate direct overrides from live seats.

The fix reads ONE shared capacity verdict (``WorkflowService
._slot_at_capacity``) in both places, so the sweep can never hand work to a
seat the cap would reject.
"""

import os
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Comment, Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import _reclaim_wip_overload, _sweep_rebalance_pods_impl

_USERS = {
    "seat@shizuha.com":    (9001, "seat@shizuha.com"),
    "healthy@shizuha.com": (9002, "healthy@shizuha.com"),
}


def _fake_user_service():
    class FakeUS:
        def get_user_by_email(self, email):
            rec = _USERS.get((email or "").strip().lower())
            return {"id": rec[0], "email": rec[1]} if rec else None

        def get_user_by_id(self, user_id):
            for uid, email in _USERS.values():
                if int(user_id) == uid:
                    return {"id": uid, "email": email}
            return None
    return FakeUS()


def _sweep_patches():
    # Mirror the PLS-190 test convention: fixture seats have no user-service
    # records or health samples, so resolution and availability fail closed.
    return (
        patch("tasks.user_service.user_service", _fake_user_service()),
        patch.object(
            WorkflowService, "_is_agent_available_for_assignment",
            return_value=True,
        ),
    )


class SharedCapacityRebalanceTests(TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "PULSE_WIP_CAP": "10",
            "PULSE_INFLIGHT_CAP": "15",
            "PULSE_WIP_CAP_TEAMS": "engineering",
            "PULSE_QUEUE_AGE_SLA_HOURS": "9999",
            "PULSE_QUEUE_STARVATION_MIN_AGE_MINUTES": "9999",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

        self.project = Project.objects.create(
            name="Pulse", key="PLS", org_id=1, access_level="open",
        )

    def _pod(self, members, *, active_wip_cap=0, ready_wip_cap=1):
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name="engineering",
            defaults={
                "organization_slug": "shizuha",
                "tiers": [members],
                "is_active": True,
                "wip_enforced": True,
                # cap 0 = no cap in roster semantics (discriminating
                # dimension is assigned-ready by default).
                "active_wip_cap": active_wip_cap,
                "ready_wip_cap": ready_wip_cap,
            },
        )
        WorkflowService._invalidate_team_roster_caches()
        self.addCleanup(WorkflowService._invalidate_team_roster_caches)

    def _item(self, key, *, status, assignee_id=None, assignee_email=None,
              metadata=None):
        # PLS-1433 review 6449: never pass None over JSONField's default=dict —
        # a valid status skips save()-coercion and inserts a NULL metadata.
        return Item.objects.create(
            project=self.project,
            item_key=key,
            title=key,
            mode="task",
            item_type="task",
            workflow_name="simple",
            workflow_status=status,
            status=status,
            assignment_group="engineering",
            assignee_id=assignee_id,
            assignee_email=assignee_email,
            metadata=metadata or {},
            created_by_email="system@pulse",
            created_by_id=0,
        )

    def test_full_on_assigned_ready_seat_is_never_a_pull_target(self):
        # Sole candidate is FULL on assigned-ready (1/1) while the legacy
        # counters show zero hands-on work — the exact desync shape.
        self._pod(["seat@shizuha.com"])
        self._item("PLS-1000", status="ready",
                   assignee_id=9001, assignee_email="seat@agents.shizuha.io")
        carrier = self._item("PLS-1001", status="open")

        patches = _sweep_patches()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for _ in range(3):
            _sweep_rebalance_pods_impl(max_skew=0)

        carrier.refresh_from_db()
        self.assertIsNone(carrier.assignee_id)
        self.assertEqual(
            (carrier.assignee_email or ""),
            "",
            "an assigned-ready-full seat must never receive the carrier",
        )
        chatter = Comment.objects.filter(item=carrier)
        self.assertEqual(
            chatter.filter(content__icontains="Pulled from").count(), 0,
            "no pull comment may be posted for a cap-rejected assignee",
        )
        self.assertEqual(
            chatter.filter(content__icontains="Returned").count(), 0,
            "no return comment may follow a pull that never happened",
        )

    def test_shared_verdict_scores_full_seat_ineligible_and_under_cap_eligible(self):
        # Control at the contract level: the shared verdict must reject the
        # assigned-ready-full seat and admit a genuinely under-cap one. (The
        # full sweep pipeline has source-gates fixture seats cannot satisfy;
        # the end-to-end no-chatter invariant is covered by the first test.)
        self._pod(["seat@shizuha.com", "healthy@shizuha.com"])
        self._item("PLS-1010", status="ready",
                   assignee_id=9011, assignee_email="seat@agents.shizuha.io")
        probe = self._item("PLS-1012", status="open")

        patches = _sweep_patches()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.assertIs(
            WorkflowService._slot_at_capacity(
                probe, "engineering", "seat@shizuha.com", user_id=9001),
            True,
            "assigned-ready-full seat must score ineligible",
        )
        self.assertIs(
            WorkflowService._slot_at_capacity(
                probe, "engineering", "healthy@shizuha.com", user_id=9002),
            False,
            "genuinely under-cap seat must score eligible",
        )

    def test_direct_override_on_live_seat_is_not_shed_by_recovery(self):
        # Over-cap seat (2 hands-on against active cap 1): the direct override
        # survives, the group-routed slack sheds.
        self._pod(["seat@shizuha.com"], active_wip_cap=1)
        pinned = self._item(
            "PLS-1020", status="in_progress",
            assignee_id=9021, assignee_email="seat@agents.shizuha.io",
            metadata={"direct_assignment": {
                "assignee_email": "seat@agents.shizuha.io",
                "reason": "operator override: critical verification",
                "at": "2026-10-08T00:00:00+00:00",
            }},
        )
        sheddable = self._item("PLS-1021", status="in_progress",
                               assignee_id=9022, assignee_email="seat@agents.shizuha.io")

        result = _reclaim_wip_overload()

        pinned.refresh_from_db()
        sheddable.refresh_from_db()
        self.assertEqual(
            (pinned.assignee_email or ""),
            "seat@agents.shizuha.io",
            "direct override on a live seat must survive recovery",
        )
        self.assertIsNone(
            sheddable.assignee_id,
            "slack must still shed when the seat is over cap",
        )
        self.assertGreaterEqual(
            result.get("active_queued", 0) + result.get("active_stopped", 0),
            1,
        )
