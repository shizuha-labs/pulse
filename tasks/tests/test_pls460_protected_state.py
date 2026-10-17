"""PLS-460 / aoi 2026-07-13: items deliberately held at the merge separation-of-
duties gate (`metadata['merge_sod_hold']`) are PROTECTED, not capacity-starved,
so `_org_health_routable_queue` must drop them from the starvation queue even
when their status is otherwise routable.
"""
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from tasks.models import Comment, Item, Project
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import (
    _org_health_merge_protected,
    _org_health_routable_queue,
    emit_org_health_signal,
    sweep_org_health,
)


def _item(status="open", metadata=None, key="X-1", due_date=None):
    return SimpleNamespace(
        status=status,
        metadata=metadata or {},
        item_key=key,
        due_date=due_date,
    )


class OrgHealthProtectedStateTests(SimpleTestCase):
    def test_active_merge_sod_hold_is_protected(self):
        held = _item(status="open", metadata={"merge_sod_hold": {"reason": "author==merger"}})
        self.assertTrue(_org_health_merge_protected(held))
        # excluded from the starve queue despite an otherwise-routable 'open' status
        self.assertEqual(_org_health_routable_queue([held]), [])

    def test_ordinary_queued_item_is_routable(self):
        it = _item(status="open", metadata={})
        self.assertFalse(_org_health_merge_protected(it))
        self.assertEqual(_org_health_routable_queue([it]), [it])

    def test_empty_hold_value_is_not_protected(self):
        # a falsy/empty hold is not an active hold — the item is still routable
        it = _item(status="open", metadata={"merge_sod_hold": {}})
        self.assertFalse(_org_health_merge_protected(it))
        self.assertEqual(_org_health_routable_queue([it]), [it])

    def test_missing_or_nondict_metadata_is_safe(self):
        for meta in (None, [], "x"):
            it = _item(status="open", metadata=meta)
            self.assertFalse(_org_health_merge_protected(it))

    def test_mixed_queue_drops_only_the_protected(self):
        held = _item(status="open", metadata={"merge_sod_hold": {"x": 1}}, key="H-1")
        ready = _item(status="open", metadata={}, key="R-1")
        self.assertEqual(_org_health_routable_queue([held, ready]), [ready])

    def test_review_and_linked_blocker_stage_holds_are_protected(self):
        ready = _item(status="open", key="R-1")
        held = [
            _item(status="blocked", key="B-1"),
            _item(status="in_review", key="V-1"),
            _item(status="awaiting_merge", key="M-1"),
        ]
        self.assertEqual(_org_health_routable_queue([*held, ready]), [ready])

    def test_future_due_generation_is_not_actionable_starvation(self):
        future = _item(
            status="open",
            key="QNT-9",
            due_date=timezone.now() + timedelta(days=7),
        )
        ready = _item(status="open", key="R-1")
        self.assertEqual(_org_health_routable_queue([future, ready]), [ready])


class _FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)


class OrgHealthDmOnlyContractTests(TestCase):
    """PLS-460: every PM detector branch terminates at one DM-only sink."""

    def setUp(self):
        self.project = Project.objects.create(
            name="Pulse", key="PLS", org_id=1, access_level="open",
        )
        self.env = patch.dict(os.environ, {
            "PULSE_QUEUE_AGE_SLA_HOURS": "1",
            "PULSE_QUEUE_STARVATION_MIN_AGE_MINUTES": "0",
            "PULSE_BACKLOG_PRESSURE_COUNT": "1",
            "PULSE_BACKLOG_AGE_SLA_HOURS": "1",
            "PULSE_THROUGHPUT_MIN_ACTIONABLE": "9999",
            "PULSE_ORG_HEALTH_DM_REPING_SECONDS": "21600",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    @staticmethod
    def _routing_snapshot(*_args, **_kwargs):
        return {
            "organization_id": 1,
            "organization_slug": "shizuha",
            "team_name": "engineering",
            "source": "team_roster",
            "source_version": "test-v1",
            "reason": "ok",
            "candidates": [{"email": "kai@shizuha.com"}],
        }

    def _run_sweep(self, exact_lanes):
        delivered = []

        def capture_post(_url, *, json, headers, timeout):
            delivered.append(json["content"])
            return SimpleNamespace(status_code=200)

        with (
            patch("tasks.tasks._org_health_exporter_fresh", return_value=False),
            patch("tasks.tasks._org_health_exact_routing_lanes", return_value=exact_lanes),
            patch("tasks.tasks._run_workflow_stale_timers", return_value=0),
            patch("tasks.tasks._record_zero_member_team_detector_success"),
            patch(
                "tasks.services.routing_authority.routing_candidate_snapshot_for_item",
                side_effect=self._routing_snapshot,
            ),
            patch.object(WorkflowService, "_disabled_agent_emails", return_value=set()),
            patch.object(
                WorkflowService, "_resolve_team_assignee",
                return_value=(9001, "shizuha@shizuha.com"),
            ),
            patch.object(WorkflowService, "_redis_client", return_value=None),
            patch("tasks.services.connect_origin.system_message_headers", return_value={}),
            patch(
                "tasks.services.admin_service.admin_service.is_org_member",
                return_value=True,
            ),
            patch("requests.post", side_effect=capture_post),
            patch("tasks.tasks.emit_org_health_signal", wraps=emit_org_health_signal) as sink,
            patch.object(Item.objects, "create") as create_item,
            patch.object(Item, "save") as save_item,
            patch.object(Item, "assign") as assign_item,
            patch.object(Comment.objects, "create") as create_comment,
            patch.object(WorkflowService, "_apply_resolved_assignee") as assign_api,
        ):
            before = list(Item.objects.values_list(
                "id", "status", "assignee_email", "assignment_group",
            ))
            result = sweep_org_health.run()
            after = list(Item.objects.values_list(
                "id", "status", "assignee_email", "assignment_group",
            ))

        self.assertEqual(after, before)
        create_item.assert_not_called()
        save_item.assert_not_called()
        assign_item.assert_not_called()
        create_comment.assert_not_called()
        assign_api.assert_not_called()
        self.assertFalse(Item.objects.filter(
            assignee_email="operator@example.test",
        ).exists())
        return result, delivered, sink

    def test_capacity_starve_emits_exactly_one_dm_and_zero_pulse_mutations(self):
        queued = _item(status="open", key="PLS-460-Q")
        queued.created_at = timezone.now() - timedelta(hours=2)
        result, delivered, sink = self._run_sweep([{
            "organization_id": 1,
            "organization_slug": "shizuha",
            "team_key": "engineering",
            "queued": [queued],
            "members": ["kai@shizuha.com"],
            "source": "team_roster",
            "source_version": "test-v1",
        }])

        self.assertEqual((result["findings"], result["filed"], result["dmed"]), (1, 0, 1))
        self.assertEqual(len(delivered), 1)
        self.assertIn("signal: capacity-starve:1:engineering", delivered[0])
        sink.assert_called_once()
        self.assertEqual(sink.call_args.args[0], "capacity-starve:1:engineering")
        self.assertIn("PLS-460-Q", sink.call_args.args[1]["title"])

    def test_backlog_pressure_emits_exactly_one_dm_and_zero_pulse_mutations(self):
        item = Item.objects.create(
            project=self.project,
            item_key="PLS-460-B",
            title="backlog pressure fixture",
            mode="task",
            item_type="task.generic",
            workflow_name="autonomous-dev",
            workflow_status="backlog",
            status="backlog",
            assignment_group="engineering",
            created_by_email="system@pulse",
            created_by_id=0,
        )
        Item.objects.filter(pk=item.pk).update(
            created_at=timezone.now() - timedelta(hours=2),
        )

        result, delivered, sink = self._run_sweep([])

        self.assertEqual((result["findings"], result["filed"], result["dmed"]), (1, 0, 1))
        self.assertEqual(len(delivered), 1)
        self.assertIn("signal: backlog-pressure:engineering", delivered[0])
        sink.assert_called_once()
        self.assertEqual(sink.call_args.args[0], "backlog-pressure:engineering")
        self.assertIn("backlog pressure", sink.call_args.args[1]["title"])

    def test_sink_dedupes_until_reping_window_expires(self):
        redis = _FakeRedis()
        delivered = []

        def capture_post(_url, *, json, headers, timeout):
            delivered.append(json["content"])
            return SimpleNamespace(status_code=200)

        kwargs = {
            "workflow_service": WorkflowService,
            "now": timezone.now(),
            "redis_client": redis,
            "reping_seconds": 21600,
        }
        payload = {"priority": "high", "title": "title", "body": "body"}
        with (
            patch.object(
                WorkflowService, "_resolve_team_assignee",
                return_value=(9001, "shizuha@shizuha.com"),
            ),
            patch("tasks.services.connect_origin.system_message_headers", return_value={}),
            patch(
                "tasks.services.admin_service.admin_service.is_org_member",
                return_value=True,
            ),
            patch("requests.post", side_effect=capture_post),
        ):
            self.assertTrue(emit_org_health_signal("capacity-starve:1:qa", payload, **kwargs))
            self.assertFalse(emit_org_health_signal("capacity-starve:1:qa", payload, **kwargs))
            redis.delete("pulse:org_health_dm:capacity-starve:1:qa")
            self.assertTrue(emit_org_health_signal("capacity-starve:1:qa", payload, **kwargs))

        self.assertEqual(len(delivered), 2)
        self.assertTrue(all("capacity-starve:1:qa" in body for body in delivered))

    def test_failed_dm_releases_claim_for_next_sweep_retry(self):
        redis = _FakeRedis()
        responses = iter([503, 200])

        def capture_post(_url, *, json, headers, timeout):
            return SimpleNamespace(status_code=next(responses))

        kwargs = {
            "workflow_service": WorkflowService,
            "now": timezone.now(),
            "redis_client": redis,
            "reping_seconds": 21600,
        }
        payload = {"priority": "high", "title": "title", "body": "body"}
        with (
            patch.object(
                WorkflowService, "_resolve_team_assignee",
                return_value=(9001, "shizuha@shizuha.com"),
            ),
            patch("tasks.services.connect_origin.system_message_headers", return_value={}),
            patch("requests.post", side_effect=capture_post),
        ):
            self.assertFalse(emit_org_health_signal("backlog-pressure:qa", payload, **kwargs))
            self.assertEqual(redis.values, {})
            self.assertTrue(emit_org_health_signal("backlog-pressure:qa", payload, **kwargs))
