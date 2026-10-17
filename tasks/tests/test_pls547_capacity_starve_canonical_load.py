"""PLS-547 regression for the org-health capacity-starve detector.

The detector must decide whether an available member has headroom with the
same canonical email-primary, two-tier load contract used by assignment and
rebalance.  The prior assignee-id-only count produced the observed 9/10 false
headroom while the router correctly counted one additional email-owned row and
refused the pull at the active cap.
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import _org_health_member_at_capacity, sweep_org_health


class CapacityStarveCanonicalLoadTests(TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "PULSE_WIP_CAP": "10",
            "PULSE_INFLIGHT_CAP": "15",
            "PULSE_WIP_CAP_TEAMS": "engineering",
            "PULSE_QUEUE_AGE_SLA_HOURS": "9999",
            "PULSE_QUEUE_STARVATION_MIN_AGE_MINUTES": "30",
            "PULSE_BACKLOG_PRESSURE_COUNT": "9999",
            "PULSE_BACKLOG_AGE_SLA_HOURS": "9999",
            "PULSE_THROUGHPUT_MIN_ACTIONABLE": "9999",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

        self.project = Project.objects.create(
            name="Pulse", key="PLS", org_id=1, access_level="open",
        )
        self.email = "haru@shizuha.com"
        self.uid = 7701
        # Explicitly disable roster WIP enforcement for the legacy env-cap
        # (PULSE_WIP_CAP / PULSE_INFLIGHT_CAP) cases. Migrations seed
        # engineering with wip_enforced=True + active_wip_cap=1; leaving that
        # in place would make PLS-667 roster-slot parity mark every multi-item
        # owner at-cap and hide the env-cap headroom these tests assert.
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name="engineering",
            defaults={
                "organization_slug": "shizuha",
                "tiers": [[self.email]],
                "is_active": True,
                "wip_enforced": False,
                "active_wip_cap": 0,
                "ready_wip_cap": 0,
            },
        )
        WorkflowService._invalidate_team_roster_caches()
        self.addCleanup(WorkflowService._invalidate_team_roster_caches)

    def _active_item(self, key, *, assignee_id, assignee_email):
        # in_progress = hands-on active work: non-terminal, not in the
        # assignment-excluded set, not review-pipeline, not an umbrella epic.
        return Item.objects.create(
            project=self.project,
            item_key=key,
            title=key,
            mode="task",
            item_type="task",
            workflow_name="simple",
            workflow_status="in_progress",
            status="in_progress",
            assignment_group="engineering",
            assignee_id=assignee_id,
            assignee_email=assignee_email,
            created_by_email="system@pulse",
            created_by_id=0,
        )

    def _prior_id_only_count(self):
        # The prior buggy predicate is retained only to prove the exact
        # divergence that produced the false page.
        excluded = set(WorkflowService._assignment_excluded_statuses())
        return (
            Item.objects
            .filter(assignee_id=self.uid, resolution__isnull=True)
            .exclude(status__in=excluded)
            .count()
        )

    def _queued_item(self, key):
        item = Item.objects.create(
            project=self.project,
            item_key=key,
            title=key,
            mode="task",
            item_type="task",
            workflow_name="simple",
            workflow_status="open",
            status="open",
            assignment_group="engineering",
            created_by_email="system@pulse",
            created_by_id=0,
        )
        Item.objects.filter(pk=item.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=1),
        )
        item.refresh_from_db()
        return item

    def _run_sweep(self, queued):
        delivered = []

        def queued_for_team(team, organization_id=None):
            self.assertEqual(organization_id, self.project.org_id)
            return [queued] if team == "engineering" else []

        def capture_post(_url, *, json, headers, timeout):
            delivered.append(json["content"])
            return SimpleNamespace(status_code=200)

        with (
            patch("tasks.tasks._org_health_exporter_fresh", return_value=True),
            patch("tasks.tasks._run_workflow_stale_timers", return_value=0),
            patch.object(WorkflowService, "_pod_roster", return_value={
                "engineering": [self.email],
                "program-management": [],
            }),
            patch.object(
                WorkflowService,
                "_queued_tasks_for_team",
                side_effect=queued_for_team,
            ),
            patch.object(
                WorkflowService,
                "_is_agent_available_for_assignment",
                return_value=True,
            ),
            patch.object(WorkflowService, "_disabled_agent_emails", return_value=set()),
            patch.object(
                WorkflowService,
                "_resolve_team_assignee",
                return_value=(9901, "shizuha@shizuha.com"),
            ),
            patch.object(WorkflowService, "_redis_client", return_value=None),
            patch(
                "tasks.user_service.user_service.get_user_by_email",
                return_value={"id": str(self.uid)},
            ),
            patch(
                "tasks.services.admin_service.admin_service.is_org_member",
                return_value=True,
            ),
            patch("requests.post", side_effect=capture_post),
        ):
            result = sweep_org_health.run()

        return result, delivered

    def _seed_active_boundary(self):
        cap = WorkflowService._wip_cap()
        for n in range(cap - 1):
            self._active_item(
                f"PLS-71{n:02d}",
                assignee_id=self.uid,
                assignee_email=self.email,
            )
        return self._active_item(
            "PLS-7199",
            assignee_id=None,
            assignee_email=self.email,
        )

    def test_email_owned_row_puts_member_at_cap_and_suppresses_false_page(self):
        cap = WorkflowService._wip_cap()
        self.assertEqual(cap, 10)
        self._seed_active_boundary()
        queued = self._queued_item("PLS-7990")

        self.assertEqual(self._prior_id_only_count(), cap - 1)
        self.assertEqual(
            WorkflowService._agent_active_load(self.uid, self.email), cap,
        )

        at_cap, active, inflight, _slots = _org_health_member_at_capacity(
            self.email, self.uid,
        )
        self.assertTrue(
            at_cap, "a member at ACTIVE_CAP must never be reported under-cap",
        )
        self.assertEqual((active, inflight), (cap, cap))

        result, delivered = self._run_sweep(queued)
        self.assertEqual(result["findings"], 0)
        self.assertEqual(result["dmed"], 0)
        self.assertEqual(delivered, [])

    def test_genuine_under_cap_member_still_detected(self):
        cap = WorkflowService._wip_cap()
        email_owned = self._seed_active_boundary()
        email_owned.delete()
        queued = self._queued_item("PLS-7991")

        self.assertEqual(
            WorkflowService._agent_active_load(self.uid, self.email), cap - 1,
        )
        at_cap, active, inflight, _slots = _org_health_member_at_capacity(
            self.email, self.uid,
        )
        self.assertFalse(at_cap)
        self.assertEqual((active, inflight), (cap - 1, cap - 1))

        result, delivered = self._run_sweep(queued)
        self.assertEqual(result["findings"], 1)
        self.assertEqual(result["dmed"], 1)
        self.assertEqual(len(delivered), 1)
        self.assertIn("capacity-starve:1:engineering", delivered[0])
        self.assertIn("active 9/10, in-flight 9/15", delivered[0])

    def test_inflight_cap_tier_also_gates(self):
        active_cap = WorkflowService._wip_cap()
        inflight_cap = WorkflowService._inflight_cap()
        self.assertEqual((active_cap, inflight_cap), (10, 15))

        review_statuses = sorted(WorkflowService._REVIEW_PIPELINE_STATUSES)
        excluded = set(WorkflowService._assignment_excluded_statuses())
        review_status = next(
            (status for status in review_statuses if status not in excluded),
            None,
        )
        self.assertIsNotNone(review_status)

        for n in range(active_cap - 1):
            self._active_item(
                f"PLS-73{n:02d}",
                assignee_id=self.uid,
                assignee_email=self.email,
            )
        for n in range(inflight_cap - (active_cap - 1)):
            item = self._active_item(
                f"PLS-74{n:02d}",
                assignee_id=self.uid,
                assignee_email=self.email,
            )
            Item.objects.filter(pk=item.pk).update(
                status=review_status,
                workflow_status=review_status,
            )

        self.assertLess(
            WorkflowService._agent_active_load(self.uid, self.email), active_cap,
        )
        self.assertGreaterEqual(
            WorkflowService._agent_workable_load(self.uid, self.email),
            inflight_cap,
        )
        at_cap, active, inflight, _slots = _org_health_member_at_capacity(
            self.email, self.uid,
        )
        self.assertTrue(
            at_cap, "a member at INFLIGHT_CAP must be treated as at capacity",
        )
        self.assertEqual((active, inflight), (active_cap - 1, inflight_cap))

        result, delivered = self._run_sweep(self._queued_item("PLS-7992"))
        self.assertEqual(result["findings"], 0)
        self.assertEqual(result["dmed"], 0)
        self.assertEqual(delivered, [])

    def test_pls667_ready_cap_full_suppresses_false_capacity_starve(self):
        """Ready-slot saturation is real capacity — not a routing desync.

        Production (research-analytics / CTX-456): members under active/in-flight
        caps but full on assigned-ready (recurring open rows) were reported as
        available under-cap, paging false capacity-starve while the router held
        the queue at ready_wip_cap.
        """
        TeamRoster.objects.filter(
            organization_id=1, team_name="engineering",
        ).update(
            wip_enforced=True,
            ready_wip_cap=1,
            active_wip_cap=10,
        )
        WorkflowService._invalidate_team_roster_caches()

        # One assigned ready item fills ready_cap=1; active/in-flight still open.
        Item.objects.create(
            project=self.project,
            item_key="PLS-667-ready",
            title="assigned ready fills ready cap",
            mode="task",
            item_type="task",
            workflow_name="simple",
            workflow_status="open",
            status="open",
            assignment_group="engineering",
            assignee_id=self.uid,
            assignee_email=self.email,
            created_by_email="system@pulse",
            created_by_id=0,
        )
        queued = self._queued_item("PLS-667-queued")

        at_cap, active, inflight, slots = _org_health_member_at_capacity(
            self.email, self.uid,
            team="engineering",
            organization_id=self.project.org_id,
        )
        self.assertLess(active, WorkflowService._wip_cap())
        self.assertLess(inflight, WorkflowService._inflight_cap())
        self.assertEqual(slots.get("ready_cap"), 1)
        self.assertEqual(slots.get("ready_load"), 1)
        self.assertTrue(
            at_cap,
            "ready-cap full must count as at-capacity for org-health (PLS-667)",
        )

        result, delivered = self._run_sweep(queued)
        self.assertEqual(result["findings"], 0)
        self.assertEqual(result["dmed"], 0)
        self.assertEqual(delivered, [])
