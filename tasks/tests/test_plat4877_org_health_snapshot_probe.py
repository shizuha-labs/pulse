"""PLAT-4877: exact-org health snapshots fail loud instead of reading zero."""
from django.core.cache import cache
from django.db import OperationalError
from django.test import SimpleTestCase, override_settings

from tasks.services.org_health_snapshot_probe import (
    CACHE_KEY,
    record_snapshot_failure,
    record_snapshot_success,
    render_org_health_snapshot_metrics,
    snapshot_probe_state,
)


@override_settings(CACHES={
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
})
class OrgHealthSnapshotProbeTests(SimpleTestCase):
    def setUp(self):
        cache.delete(CACHE_KEY)

    def test_failure_before_first_lane_is_unhealthy_not_zero(self):
        state = record_snapshot_failure(OperationalError("down"), now=100)

        self.assertFalse(state["success"])
        self.assertEqual(state["error"], "database")
        self.assertEqual(state["attempted"], 0)
        self.assertEqual(state["completed"], 0)
        self.assertEqual(state["last_success_timestamp"], 0)
        body = render_org_health_snapshot_metrics(now=110)
        self.assertIn('pulse_org_health_exact_snapshot_success{source="org_health_exact_routing_lanes"} 0', body)
        self.assertIn('organization_id="unknown",team="unknown",source="unknown",version="unknown"} 0', body)

    def test_partial_failure_preserves_progress_and_last_success(self):
        record_snapshot_success(attempted=1, completed=1, lanes=[], now=50)
        state = record_snapshot_failure(
            RuntimeError("bad lane"), attempted=2, completed=1, now=80,
            lanes=[{
                "organization_id": 1, "team_key": "engineering",
                "source": "routing_binding", "version": "team:2", "success": True,
            }],
            failed_lane={
                "organization_id": 7, "team_key": "review",
                "source": "unknown", "version": "unknown",
            },
        )

        self.assertEqual(state["last_success_timestamp"], 50)
        self.assertEqual((state["attempted"], state["completed"]), (2, 1))
        body = render_org_health_snapshot_metrics(now=90)
        self.assertIn('phase="attempted",source="org_health_exact_routing_lanes"} 2', body)
        self.assertIn('phase="completed",source="org_health_exact_routing_lanes"} 1', body)
        self.assertIn('organization_id="7",team="review",source="unknown",version="unknown"} 0', body)
        self.assertIn('pulse_org_health_exact_snapshot_last_success_age_seconds{source="org_health_exact_routing_lanes"} 40', body)

    def test_real_zero_lane_snapshot_is_successful(self):
        state = record_snapshot_success(attempted=0, completed=0, lanes=[], now=200)

        self.assertTrue(state["success"])
        self.assertEqual(state["last_success_timestamp"], 200)
        body = render_org_health_snapshot_metrics(now=205)
        self.assertIn('pulse_org_health_exact_snapshot_success{source="org_health_exact_routing_lanes"} 1', body)
        self.assertNotIn('team="unknown"', body)

    def test_recovery_clears_error_and_advances_last_success(self):
        record_snapshot_failure(TimeoutError("late"), now=300)
        recovered = record_snapshot_success(
            attempted=1, completed=1, now=320,
            lanes=[{
                "organization_id": 1, "team_key": "devops",
                "source": "team_roster_compat", "version": "row:4", "success": True,
            }],
        )

        self.assertEqual(snapshot_probe_state(), recovered)
        self.assertEqual(recovered["error"], "none")
        self.assertEqual(recovered["last_success_timestamp"], 320)
        body = render_org_health_snapshot_metrics(now=325)
        self.assertIn('error="none",source="org_health_exact_routing_lanes"} 1', body)
        self.assertIn('organization_id="1",team="devops",source="team_roster_compat",version="row:4"} 1', body)
