"""PLAT-1197 company-state snapshot API tests."""

import json
import os
import re
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import MagicMock, patch
from uuid import uuid4

import requests
from django.core.cache import cache
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from tasks.models import Comment, Item, ItemLink, Project
from tasks.services.company_state import (
    BOOKS_LKG,
    BOOKS_LKG_MAX_ENTRIES,
    CompanyStateSnapshotService,
    SnapshotOrg,
)
from tasks.views import CompanyStateSnapshotView


def _section(state="ok", data=None, as_of=None):
    now = as_of or timezone.now()
    return {
        "state": state,
        "as_of": now.isoformat() if state != "not_configured" else None,
        "stale_after_seconds": 3600,
        "sources": [{"system": "pulse", "ref": "test"}],
        "errors": [] if state == "ok" else [{"source": "test", "message": "failed"}],
        "data": {} if data is None else data,
    }


def _json_response(payload, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class CompanyStateSnapshotServiceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    @patch.object(CompanyStateSnapshotService, "_pulse_flow_section")
    @patch.object(CompanyStateSnapshotService, "_queues_section")
    @patch.object(CompanyStateSnapshotService, "_alerts_incidents_section")
    @patch.object(CompanyStateSnapshotService, "_product_backlog_section")
    @patch.object(CompanyStateSnapshotService, "_learning_loop_section")
    @patch.object(CompanyStateSnapshotService, "_executive_directives_section")
    def test_snapshot_schema_and_degraded_partial_source_are_explicit(
        self,
        directives,
        learning,
        product,
        alerts,
        queues,
        flow,
        org,
    ):
        org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")
        flow.return_value = _section(data={"completed": {"last_24h": 3}})
        queues.return_value = _section(data={"active_total": 9})
        alerts.return_value = _section(
            state="degraded",
            data={
                "active_alerts_by_severity": None,
                "open_pulse_incidents": 0,
                "alertmanager": None,
            },
        )
        product.return_value = _section(data={"project_count": 2})
        learning.return_value = _section(state="degraded", data={"conversion_rate": None})
        directives.return_value = _section(data={"top_directives": []})

        with patch.object(
            CompanyStateSnapshotService, "_cortex_section",
            return_value=_section(state="degraded", data={"tokens_last_24h": None}),
        ), patch.object(
            CompanyStateSnapshotService, "_hive_fleet_section",
            return_value=_section(state="degraded", data={"fleet": None}),
        ):
            snapshot = CompanyStateSnapshotService(org_slug="shizuha").build_snapshot()

        self.assertEqual(snapshot["schema_version"], "company_state.snapshot.v1")
        self.assertEqual(snapshot["org"]["slug"], "shizuha")
        self.assertEqual(snapshot["overall_state"], "degraded")
        self.assertIn("pulse_flow", snapshot["sections"])
        # No-silent-zero: unavailable Alertmanager-derived alert counts remain null,
        # while a Pulse-backed zero incident count stays a real zero.
        alert_data = snapshot["sections"]["alerts_incidents"]["data"]
        self.assertIsNone(alert_data["active_alerts_by_severity"])
        self.assertEqual(alert_data["open_pulse_incidents"], 0)
        self.assertTrue(snapshot["source_status"])


class CompanyStateAlertsIncidentsTests(TestCase):
    def setUp(self):
        cache.clear()

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_alertmanager_unavailable_does_not_silent_zero_firing_alerts(self, org):
        org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")
        project = Project.objects.create(org_id=1, key="ALERT", name="Alert project")
        Item.objects.create(
            mode="alert",
            title="Critical test alert",
            status="firing",
            severity="critical",
            assignment_group="devops",
            project=project,
        )
        Item.objects.create(
            mode="task",
            title="Open incident",
            status="in_progress",
            workflow_name="incident",
            project=project,
        )

        with patch(
            "tasks.services.company_state.requests.get",
            side_effect=requests.Timeout("deadline"),
        ) as get:
            section = CompanyStateSnapshotService(org_slug="shizuha")._alerts_incidents_section()

        self.assertEqual(section["state"], "degraded")
        data = section["data"]
        self.assertIsNone(data["active_alerts_by_severity"])
        self.assertIsNone(data["active_alerts_by_owner_group"])
        self.assertIsNone(data["alertmanager"])
        self.assertEqual(data["pulse_alert_tasks_by_severity"], [{"severity": "critical", "count": 1}])
        self.assertEqual(data["pulse_alert_tasks_by_owner_group"], [{"assignment_group": "devops", "count": 1}])
        self.assertEqual(data["open_pulse_incidents"], 1)
        self.assertIsNone(section["as_of"])
        self.assertLessEqual(get.call_args.kwargs["timeout"], 2.0)

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_alertmanager_success_empty_is_a_real_zero(self, org):
        org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")

        with patch("tasks.services.company_state.requests.get", return_value=_json_response([])):
            section = CompanyStateSnapshotService(org_slug="shizuha")._alerts_incidents_section()

        self.assertEqual(section["state"], "ok")
        self.assertEqual(section["data"]["active_alerts_total"], 0)
        self.assertEqual(section["data"]["active_alerts_by_severity"], [])
        self.assertEqual(section["data"]["active_alerts_by_owner_group"], [])
        self.assertEqual(section["data"]["active_alerts_by_alertname"], [])

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_alertmanager_malformed_payload_is_unknown_not_zero(self, org):
        org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")

        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response({"alerts": []}),
        ):
            section = CompanyStateSnapshotService(org_slug="shizuha")._alerts_incidents_section()

        self.assertEqual(section["state"], "degraded")
        self.assertIsNone(section["data"]["active_alerts_total"])
        self.assertEqual(section["errors"][0]["related_task"], "PLAT-4775")



class CompanyStateTelemetryAdapterTests(TestCase):
    HIVE_HEALTH_METRICS = (
        "shizuha_agent_provider_healthy",
        "shizuha_agent_auth_failed",
        "shizuha_agent_bridge_auth_unavailable",
        "shizuha_agent_mcp_errors",
        "shizuha_agent_wedge_errors",
        "shizuha_agent_empty_turn_wedge",
        "shizuha_agent_pulse_wedge",
        "shizuha_agent_capacity_unavailable",
    )

    def setUp(self):
        cache.clear()
        self.now = timezone.now()

    def _org(self):
        return patch.object(
            CompanyStateSnapshotService,
            "_resolve_org",
            return_value=SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd"),
        )

    def _prometheus_response(self, query, *, empty_field=None):
        if empty_field and empty_field in query:
            result = []
        elif "label_replace(min(timestamp(" in query:
            families = re.findall(r"min\(timestamp\(([^)]+)\)\)", query)
            result = [
                {
                    "metric": {"family": family},
                    "value": [self.now.timestamp(), str(self.now.timestamp())],
                }
                for family in families
            ]
        elif "topk(" in query:
            result = [{
                "metric": {"model_name": "test-model"},
                "value": [self.now.timestamp(), "7"],
            }]
        else:
            result = [{"metric": {}, "value": [self.now.timestamp(), "0"]}]
        return _json_response({"status": "success", "data": {"result": result}})

    def _complete_hive_metrics(self):
        agents = {
            "ryo": {"enabled": 1, "up": 1, "provider": 1, "wedge": 0},
            "stopped": {"enabled": 0, "up": 0, "provider": 1, "wedge": 0},
            "wedged": {"enabled": 1, "up": 1, "provider": 0, "wedge": 2},
        }
        lines = [f"shizuha_exporter_last_gather_timestamp_seconds {self.now.timestamp()}"]
        for agent, values in agents.items():
            lines.extend([
                f'shizuha_agent_enabled{{agent="{agent}"}} {values["enabled"]}',
                f'shizuha_agent_up{{agent="{agent}"}} {values["up"]}',
            ])
            for metric in self.HIVE_HEALTH_METRICS:
                value = 0
                if metric == "shizuha_agent_provider_healthy":
                    value = values["provider"]
                elif metric == "shizuha_agent_wedge_errors":
                    value = values["wedge"]
                lines.append(f'{metric}{{agent="{agent}"}} {value}')
        return lines

    def test_prometheus_real_zero_and_empty_vector_remain_distinct(self):
        def real_zero(_url, *, params, timeout):
            return self._prometheus_response(params["query"])

        with self._org(), patch(
            "tasks.services.company_state.requests.get", side_effect=real_zero,
        ):
            real = CompanyStateSnapshotService(org_slug="shizuha", now=self.now)._cortex_section()
        self.assertEqual(real["data"]["degraded_backends"], 0)

        def empty_vector(_url, *, params, timeout):
            return self._prometheus_response(
                params["query"], empty_field="sum(1 - clamp_max",
            )

        with self._org(), patch(
            "tasks.services.company_state.requests.get", side_effect=empty_vector,
        ):
            empty = CompanyStateSnapshotService(org_slug="shizuha", now=self.now)._cortex_section()
        self.assertEqual(empty["state"], "degraded")
        self.assertIsNone(empty["data"]["degraded_backends"])
        self.assertTrue(any("empty vector" in error["message"] for error in empty["errors"]))

    def test_prometheus_queries_share_one_decreasing_source_deadline(self):
        timeouts = []

        def response(_url, *, params, timeout):
            timeouts.append(timeout)
            return self._prometheus_response(params["query"])

        ticks = iter([0.0] + [index * 0.05 for index in range(1, 10)])
        with self._org(), patch.dict(
            os.environ, {"COMPANY_STATE_SOURCE_TIMEOUT_SECONDS": "1"}, clear=False,
        ), patch(
            "tasks.services.company_state.time.monotonic", side_effect=lambda: next(ticks),
        ), patch(
            "tasks.services.company_state.requests.get", side_effect=response,
        ):
            CompanyStateSnapshotService(org_slug="shizuha", now=self.now)._cortex_section()
        self.assertEqual(len(timeouts), 9)
        self.assertLess(timeouts[-1], timeouts[0])
        self.assertTrue(all(0 < timeout <= 1 for timeout in timeouts))

    def _hive_payload(self, *, unknown=False):
        def family(value):
            return {
                "known": not unknown,
                "value": None if unknown else value,
                "observed_at": self.now.isoformat() if not unknown else None,
                "source": "test" if not unknown else None,
            }

        agents = [
            {
                "username": "ready",
                "provider_healthy": family(True),
                "auth_failed": family(False),
                "bridge_auth_unavailable": family(False),
                "mcp_errors": family(0),
                "wedge_errors": family(0),
                "empty_turn_wedge": family(False),
                "pulse_wedge": family(False),
                "capacity_unavailable": family(False),
            },
            {
                "username": "wedged",
                "provider_healthy": family(False),
                "auth_failed": family(False),
                "bridge_auth_unavailable": family(False),
                "mcp_errors": family(0),
                "wedge_errors": family(2),
                "empty_turn_wedge": family(False),
                "pulse_wedge": family(False),
                "capacity_unavailable": family(False),
            },
        ]
        return {
            "schema": "hive.company_state.fleet.v1",
            "schema_version": "hive.company_state.fleet.v1",
            "observed_at": self.now.isoformat(),
            "scope": "platform_aggregate",
            "tenants": {
                "total": 2, "active": 1, "trial": 1, "active_or_trial": 2,
                "by_status": {"active": 1, "trial": 1},
            },
            "fleet": {"enabled": 2, "running": 2, "stopped": 0},
            "agents": agents,
        }

    def _hive_section(self, payload):
        response = _json_response(payload)
        with self._org(), patch.dict(os.environ, {
            "COMPANY_STATE_HIVE_URL": "http://hive.test/api/internal/company-state/fleet/v1/",
            "COMPANY_STATE_HIVE_TOKEN": "dedicated-read-token",
        }, clear=False), patch(
            "tasks.services.company_state.requests.get", return_value=response,
        ) as get:
            section = CompanyStateSnapshotService(
                org_slug="shizuha", now=self.now,
            )._hive_fleet_section()
        get.assert_called_once_with(
            "http://hive.test/api/internal/company-state/fleet/v1/",
            params={"org_slug": "shizuha"},
            headers={"Authorization": "Bearer dedicated-read-token"},
            timeout=2.0,
        )
        return section

    def test_hive_company_state_live_counts_and_tenant_aggregate(self):
        section = self._hive_section(self._hive_payload())
        self.assertEqual(section["state"], "ok")
        self.assertEqual(section["data"]["fleet"], {
            "enabled": 2, "running": 2, "available": 1, "stopped": 0, "unavailable": 1,
        })
        self.assertEqual(section["data"]["provider_down_agents"], ["wedged"])
        self.assertEqual(section["data"]["wedged_agents"], ["wedged"])
        self.assertEqual(section["data"]["tenant_count"], 2)
        self.assertEqual(section["data"]["tenant_summary"]["active_or_trial"], 2)

    def test_hive_unknown_agent_health_is_not_silent_zero(self):
        section = self._hive_section(self._hive_payload(unknown=True))
        self.assertEqual(section["state"], "degraded")
        self.assertIsNone(section["data"]["fleet"]["available"])
        self.assertIsNone(section["data"]["fleet"]["unavailable"])
        self.assertIsNone(section["data"]["provider_down_agents"])
        self.assertIn("unknown agent families", section["errors"][0]["message"])

    def test_hive_missing_token_fails_closed_without_network_call(self):
        with self._org(), patch.dict(os.environ, {
            "COMPANY_STATE_HIVE_URL": "http://hive.test/api/internal/company-state/fleet/v1/",
            "COMPANY_STATE_HIVE_TOKEN": "",
        }, clear=False), patch("tasks.services.company_state.requests.get") as get:
            section = CompanyStateSnapshotService(
                org_slug="shizuha", now=self.now,
            )._hive_fleet_section()
        self.assertEqual(section["state"], "unavailable")
        self.assertIsNone(section["data"])
        self.assertEqual(section["errors"][0]["related_task"], "HIVE-1878")
        get.assert_not_called()

    def test_hive_second_org_not_configured_never_returns_platform_data(self):
        payload = {
            "schema": "hive.company_state.fleet.v1",
            "schema_version": "hive.company_state.fleet.v1",
            "observed_at": self.now.isoformat(),
            "scope": "not_configured",
            "status": "not_configured",
        }
        section = self._hive_section(payload)
        self.assertEqual(section["state"], "not_configured")
        self.assertIsNone(section["data"])

    def test_prometheus_derived_value_is_not_fresh_when_raw_family_is_stale(self):
        stale = self.now - timezone.timedelta(seconds=301)

        def response(_url, *, params, timeout):
            query = params["query"]
            payload = self._prometheus_response(query)
            if "label_replace(min(timestamp(" in query:
                for row in payload.json()["data"]["result"]:
                    if row["metric"]["family"] == "vllm:request_success_total":
                        row["value"] = [self.now.timestamp(), str(stale.timestamp())]
            return payload

        with self._org(), patch(
            "tasks.services.company_state.requests.get", side_effect=response,
        ):
            section = CompanyStateSnapshotService(
                org_slug="shizuha", now=self.now,
            )._cortex_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIsNone(section["data"]["requests_last_24h"])
        self.assertEqual(section["data"]["tokens_last_24h"], 0)
        self.assertTrue(any(
            "source freshness invalid for requests_last_24h" in error["message"]
            for error in section["errors"]
        ))

    def test_global_sources_are_withheld_from_second_org(self):
        with patch.object(
            CompanyStateSnapshotService,
            "_resolve_org",
            return_value=SnapshotOrg(id=2, slug="other", name="Other"),
        ), patch("tasks.services.company_state.requests.get") as get:
            service = CompanyStateSnapshotService(org_slug="other", now=self.now)
            cortex = service._cortex_section()
            hive = service._hive_fleet_section()
        self.assertEqual(cortex["state"], "not_configured")
        self.assertEqual(hive["state"], "not_configured")
        self.assertEqual(cortex["errors"][0]["related_task"], "PLAT-4139")
        get.assert_not_called()

    def test_source_sample_staleness_is_normalized(self):
        with self._org():
            service = CompanyStateSnapshotService(org_slug="shizuha", now=self.now)
        old = self.now - timezone.timedelta(seconds=301)
        required = _section(state="ok", as_of=old)
        required["stale_after_seconds"] = 300
        optional = _section(state="ok", as_of=old)
        optional["stale_after_seconds"] = 300
        required = service._normalize_freshness("alerts_incidents", required)
        optional = service._normalize_freshness("cortex", optional)
        self.assertEqual(required["state"], "stale")
        self.assertEqual(optional["state"], "degraded")


class CompanyStateExecutiveDirectiveTests(TestCase):
    def setUp(self):
        cache.clear()
        self.project = Project.objects.create(org_id=1, key="PLAT", name="Platform")

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_current_cycle_minute_and_explicit_priority_order(self, org):
        org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")
        cycle = Item.objects.create(
            mode="task", item_key="PLAT-1189", title="Executive Cycle",
            status="scheduled", project=self.project,
        )
        Comment.objects.create(
            item=cycle, author_id=1, author_email="shizuha@shizuha.com",
            content=(
                "**Cycle #18 complete.** Top three for the next boundary: "
                "(1) LOW-1 low; (2) HIGH-1 high; (3) URG-1 urgent. "
                "Canonical minute: Evolution wiki page "
                "`53116f0f-8651-4825-8494-38b7efbb40cd`, version 11."
            ),
        )
        Comment.objects.create(
            item=cycle, author_id=1, author_email="shizuha@shizuha.com",
            content="cycle 18 complete — next due 2026-07-17 07:07 IST",
        )
        for key, priority in (("LOW-1", "low"), ("HIGH-1", "high"), ("URG-1", "urgent")):
            Item.objects.create(
                mode="task", item_key=key, title=key, status="open", priority=priority,
                labels=["executive-directive"], project=self.project,
            )
        section = CompanyStateSnapshotService(org_slug="shizuha")._executive_directives_section()
        self.assertEqual(section["state"], "ok")
        self.assertEqual(
            [row["item_key"] for row in section["data"]["top_directives"]],
            ["URG-1", "HIGH-1", "LOW-1"],
        )
        self.assertEqual(
            section["data"]["directive_priority_order"],
            ["urgent", "high", "normal", "low"],
        )
        self.assertEqual(section["data"]["latest_minutes_ref"]["version"], 11)
        self.assertEqual(
            section["data"]["latest_minutes_ref"]["page_id"],
            "53116f0f-8651-4825-8494-38b7efbb40cd",
        )


@skipUnless(connection.vendor == "postgresql", "PostgreSQL trigger breach harness")
class CompanyStateLinkedWorkCorruptRowTests(TransactionTestCase):
    """The read boundary stays closed even if the write invariant is breached."""

    trigger_name = "tasks_item_link_org_guard"

    def _trigger_state(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT tgenabled
                FROM pg_trigger
                WHERE tgrelid = 'tasks_item_link'::regclass
                  AND tgname = %s
                """,
                [self.trigger_name],
            )
            return cursor.fetchone()

    def _enable_trigger(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE tasks_item_link ENABLE TRIGGER tasks_item_link_org_guard"
            )

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_related_work_excludes_cross_org_and_projectless_corrupt_links(self, org):
        org.return_value = SnapshotOrg(
            id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd",
        )
        project = Project.objects.create(org_id=1, key="PLAT", name="Platform")
        cycle = Item.objects.create(
            mode="task", item_key="PLAT-1189", title="Executive Cycle",
            status="scheduled", workflow_name="simple", project=project,
        )
        directive = Item.objects.create(
            mode="task", item_key="PLAT-4775", title="Directive", status="open",
            workflow_name="simple", priority="urgent",
            labels=["executive-directive"], project=project,
        )
        same_org = Item.objects.create(
            mode="task", item_key="PLAT-4776", title="Same org", status="open",
            workflow_name="simple", project=project,
        )
        other_project = Project.objects.create(
            org_id=2, key="OTHER", name="Other org",
        )
        other_org = Item.objects.create(
            mode="task", item_key="OTHER-1", title="Other org", status="open",
            workflow_name="simple", project=other_project,
        )
        projectless = Item.objects.create(
            mode="task", item_key="NONE-1", title="Projectless", status="open",
            workflow_name="simple",
        )
        ItemLink.objects.create(
            source_item=directive,
            target_item=same_org,
            link_type="relates_to",
            created_by_id=1,
            created_by_email="test@shizuha.com",
        )
        Comment.objects.create(
            item=cycle,
            author_id=1,
            author_email="shizuha@shizuha.com",
            content=(
                "**Cycle #18 complete.** Top three for the next boundary: "
                "(1) PLAT-4775 urgent. Canonical minute: Evolution wiki page "
                "`53116f0f-8651-4825-8494-38b7efbb40cd`, version 11."
            ),
        )

        # Register recovery before the exact-trigger DDL. The finally block is
        # the primary recovery; addCleanup is the fallback for assertion/errors.
        self.addCleanup(self._enable_trigger)
        self.assertEqual(self._trigger_state(), ("O",))
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE tasks_item_link "
                "DISABLE TRIGGER tasks_item_link_org_guard"
            )
        try:
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO tasks_item_link
                        (id, source_item_id, target_item_id, link_type,
                         created_by_id, created_by_email, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            str(uuid4()), directive.id, related.id, "relates_to",
                            1, "test@shizuha.com", timezone.now(),
                        )
                        for related in (other_org, projectless)
                    ],
                )
        finally:
            self._enable_trigger()

        self.assertEqual(self._trigger_state(), ("O",))
        section = CompanyStateSnapshotService(
            org_slug="shizuha",
        )._executive_directives_section()
        related_work = section["data"]["top_directives"][0]["related_work"]
        self.assertEqual(related_work, ["PLAT-4776"])
        self.assertNotIn("OTHER-1", related_work)
        self.assertNotIn("NONE-1", related_work)


class CompanyStateOrgScopeTests(TestCase):
    """PLAT-1197 review P1: unresolved org_slug must fail closed, never
    aggregate cross-org data."""

    def setUp(self):
        cache.clear()
        self.org1_project = Project.objects.create(
            org_id=1, key="ORGA", name="Org 1 project")
        self.org2_project = Project.objects.create(
            org_id=2, key="ORGB", name="Org 2 project")
        Item.objects.create(
            mode="task", title="org1 task", status="in_progress",
            project=self.org1_project)
        Item.objects.create(
            mode="task", title="org2 task", status="in_progress",
            project=self.org2_project)
        Item.objects.create(mode="task", title="projectless task", status="open")

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_unresolved_org_does_not_aggregate_any_items(self, org):
        org.return_value = SnapshotOrg(
            id=None, slug="missing", name=None, degraded=True,
            error="admin organization lookup unavailable")
        service = CompanyStateSnapshotService(org_slug="missing")

        self.assertEqual(service._items().count(), 0)
        self.assertEqual(service._tasks().count(), 0)

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_unresolved_org_snapshot_is_unavailable_with_no_data(self, org):
        org.return_value = SnapshotOrg(
            id=None, slug="missing", name=None, degraded=True,
            error="admin organization lookup unavailable")

        snapshot = CompanyStateSnapshotService(org_slug="missing").build_snapshot()

        self.assertEqual(snapshot["overall_state"], "unavailable")
        self.assertEqual(snapshot["org"], {"id": None, "slug": "missing", "name": None})
        self.assertEqual(
            set(snapshot["sections"]),
            {"pulse_flow", "queues", "alerts_incidents", "product_backlog",
             "cortex", "hive_fleet", "spend_proxies", "learning_loop",
             "executive_directives"},
        )
        for name, section in snapshot["sections"].items():
            self.assertEqual(section["state"], "unavailable", name)
            self.assertIsNone(section["data"], name)
            self.assertTrue(section["errors"], name)

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_resolved_org_excludes_other_org_and_projectless_items(self, org):
        org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")
        service = CompanyStateSnapshotService(org_slug="shizuha")

        titles = set(service._items().values_list("title", flat=True))

        self.assertEqual(titles, {"org1 task"})
        self.assertNotIn("org2 task", titles)
        self.assertNotIn("projectless task", titles)

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_stage_dwell_excludes_other_orgs(self, org):
        """Review P1 (round 2): stage_metrics.team_throughput must not bypass
        the org scope — a resolved org-1 snapshot's stage_dwell_by_team_status
        must exclude org 2's transition history."""
        from datetime import timedelta

        from django.utils import timezone as tz

        from tasks.models import ItemActivity
        from tasks.signals import TRACKED_FIELDS

        org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")
        now = tz.now()
        org1_item = Item.objects.get(title="org1 task")
        org2_item = Item.objects.get(title="org2 task")
        Item.objects.filter(pk=org1_item.pk).update(assignment_group="org1-team")
        Item.objects.filter(pk=org2_item.pk).update(assignment_group="org2-team")
        for item in (org1_item, org2_item):
            first = ItemActivity.objects.create(
                item=item, action="status_changed",
                field_name=TRACKED_FIELDS["status"],
                old_value="open", new_value="in_progress",
                user_id=1, user_email="test@shizuha.com",
            )
            ItemActivity.objects.filter(pk=first.pk).update(
                created_at=now - timedelta(days=2))
            ItemActivity.objects.create(
                item=item, action="status_changed",
                field_name=TRACKED_FIELDS["status"],
                old_value="in_progress", new_value="done",
                user_id=1, user_email="test@shizuha.com",
            )

        section = CompanyStateSnapshotService(org_slug="shizuha")._pulse_flow_section()

        teams = {row["team"] for row in section["data"]["stage_dwell_by_team_status"]}
        self.assertIn("org1-team", teams)
        self.assertNotIn("org2-team", teams)

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_unresolved_org_returns_503_from_view(self, org):
        org.return_value = SnapshotOrg(
            id=None, slug="missing", name=None, degraded=True,
            error="admin organization lookup unavailable")
        factory = APIRequestFactory()
        request = factory.get(
            "/api/internal/company-state/snapshot/?schema_version=1&org_slug=missing")
        force_authenticate(
            request,
            user=SimpleNamespace(
                id=1, email="agent@example.com", is_authenticated=True,
                is_staff=False, is_superuser=False, organization_memberships={}),
            token={"scopes": ["pulse:company-state.read"]},
        )

        response = CompanyStateSnapshotView.as_view()(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["overall_state"], "unavailable")


class CompanyStateSnapshotViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()

    def _user(self, **kwargs):
        base = {
            "id": 1,
            "email": "agent@example.com",
            "is_authenticated": True,
            "is_staff": False,
            "is_superuser": False,
            "organization_memberships": {},
        }
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_forbids_plain_member_without_scope_or_lead_role(self):
        request = self.factory.get("/api/internal/company-state/snapshot/?schema_version=1")
        force_authenticate(request, user=self._user(organization_memberships={1: "member"}))

        response = CompanyStateSnapshotView.as_view()(request)

        self.assertEqual(response.status_code, 403)

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_lead_role_in_other_org_cannot_read_requested_org(self, resolve_org):
        """PLAT-1197 review P1: role-based access must be scoped to the
        REQUESTED org — a lead/admin in org 2 has no grant on org 1's data."""
        resolve_org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")
        request = self.factory.get(
            "/api/internal/company-state/snapshot/?schema_version=1&org_slug=shizuha")
        force_authenticate(request, user=self._user(organization_memberships={2: "lead"}))

        response = CompanyStateSnapshotView.as_view()(request)

        self.assertEqual(response.status_code, 403)

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    @patch.object(CompanyStateSnapshotService, "build_snapshot")
    def test_lead_role_in_requested_org_can_read_it(self, build_snapshot, resolve_org):
        resolve_org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")
        build_snapshot.return_value = {
            "schema_version": "company_state.snapshot.v1",
            "generated_at": timezone.now().isoformat(),
            "org": {"id": 1, "slug": "shizuha", "name": "Shizuha Global Pvt. Ltd"},
            "freshness_slo_seconds": 3600,
            "overall_state": "ok",
            "sections": {"pulse_flow": _section()},
            "source_status": [],
            "missing_inputs": [],
        }
        request = self.factory.get(
            "/api/internal/company-state/snapshot/?schema_version=1&org_slug=shizuha")
        force_authenticate(request, user=self._user(organization_memberships={1: "lead"}))

        response = CompanyStateSnapshotView.as_view()(request)

        self.assertEqual(response.status_code, 200)

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_string_keyed_membership_matches_resolved_org(self, resolve_org):
        """organization_memberships keys can arrive as strings from JWT claims."""
        resolve_org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")
        request = self.factory.get(
            "/api/internal/company-state/snapshot/?schema_version=1&org_slug=shizuha")
        force_authenticate(request, user=self._user(organization_memberships={"1": "admin"}))

        with patch.object(CompanyStateSnapshotService, "build_snapshot", return_value={
            "schema_version": "company_state.snapshot.v1",
            "generated_at": timezone.now().isoformat(),
            "org": {"id": 1, "slug": "shizuha", "name": "Shizuha Global Pvt. Ltd"},
            "freshness_slo_seconds": 3600,
            "overall_state": "ok",
            "sections": {"pulse_flow": _section()},
            "source_status": [],
            "missing_inputs": [],
        }):
            response = CompanyStateSnapshotView.as_view()(request)

        self.assertEqual(response.status_code, 200)

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_lead_role_cannot_use_unresolved_org(self, resolve_org):
        """Role-based access to an org that did not positively resolve is denied
        (fail-closed): there is no org to hold a lead role in."""
        resolve_org.return_value = SnapshotOrg(
            id=None, slug="missing", name=None, degraded=True, error="not found")
        request = self.factory.get(
            "/api/internal/company-state/snapshot/?schema_version=1&org_slug=missing")
        force_authenticate(request, user=self._user(organization_memberships={1: "lead"}))

        response = CompanyStateSnapshotView.as_view()(request)

        self.assertEqual(response.status_code, 403)

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    @patch.object(CompanyStateSnapshotService, "build_snapshot")
    def test_allows_company_state_scope_and_emits_cache_headers(self, build_snapshot, resolve_org):
        resolve_org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")
        build_snapshot.return_value = {
            "schema_version": "company_state.snapshot.v1",
            "generated_at": timezone.now().isoformat(),
            "org": {"id": 1, "slug": "shizuha", "name": "Shizuha Global Pvt. Ltd"},
            "freshness_slo_seconds": 3600,
            "overall_state": "ok",
            "sections": {"pulse_flow": _section()},
            "source_status": [],
            "missing_inputs": [],
        }
        request = self.factory.get("/api/internal/company-state/snapshot/?schema_version=1&org_slug=shizuha")
        force_authenticate(
            request,
            user=self._user(),
            token={"scopes": ["pulse:company-state.read"]},
        )

        response = CompanyStateSnapshotView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["schema_version"], "company_state.snapshot.v1")
        self.assertIn("ETag", response.headers)
        self.assertEqual(response.headers["Cache-Control"], "private, max-age=300")


class CompanyStateSnapshotFrontDoorRouteTests(TestCase):
    """PLAT-4240: full-stack route regression through the REAL caller sequence.

    PLAT-4139 shipped green tests by invoking the view directly with
    APIRequestFactory, which bypasses both the urlconf and
    InternalApiIngressGuardMiddleware — production requests through the public
    ingress carry X-Forwarded-For and were 404-masked by the guard before the
    view ever ran. These tests exercise urlconf + middleware + view auth in
    production order (django.test.Client), with the ingress condition
    explicitly simulated, per the verify-before-push stateful/caller-sequence
    doctrine (PLAT-4521).
    """

    INGRESS = {"HTTP_X_FORWARDED_FOR": "203.0.113.7"}
    SNAPSHOT = {
        "schema_version": "company_state.snapshot.v1",
        "generated_at": "2026-07-13T00:00:00+00:00",
        "org": {"id": 1, "slug": "shizuha", "name": "Shizuha Global Pvt. Ltd"},
        "freshness_slo_seconds": 3600,
        "overall_state": "ok",
        "sections": {},
        "source_status": [],
        "missing_inputs": [],
    }

    def setUp(self):
        cache.clear()
        from django.contrib.auth import get_user_model
        self.staff = get_user_model().objects.create_user(
            username="plat4240-staff", password="x", is_staff=True,
        )

    def _mock_service(self):
        resolve = patch.object(
            CompanyStateSnapshotService, "_resolve_org",
            return_value=SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd"),
        )
        build = patch.object(
            CompanyStateSnapshotService, "build_snapshot", return_value=dict(self.SNAPSHOT),
        )
        return resolve, build

    def test_frontdoor_route_serves_schema_v1_json_through_ingress(self):
        """The canonical front door returns snapshot JSON for an authorized
        caller even when the request arrives via the public ingress."""
        self.client.force_login(self.staff)
        resolve, build = self._mock_service()
        with resolve, build:
            response = self.client.get(
                "/api/company-state/snapshot/?schema_version=1&org_slug=shizuha",
                **self.INGRESS,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"].split(";")[0], "application/json")
        self.assertEqual(response.json()["schema_version"], "company_state.snapshot.v1")

    def test_frontdoor_route_via_shizuha_pulse_prefix(self):
        """Both public mounts of tasks.urls expose the front door."""
        self.client.force_login(self.staff)
        resolve, build = self._mock_service()
        with resolve, build:
            response = self.client.get(
                "/shizuha-pulse/api/company-state/snapshot/?schema_version=1&org_slug=shizuha",
                **self.INGRESS,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["schema_version"], "company_state.snapshot.v1")

    def test_frontdoor_unauthenticated_fails_closed_but_is_not_masked(self):
        """Route exists publicly: anonymous ingress traffic gets an auth error
        (401/403) — never the guard's masked 404 and never an SPA/HTML page."""
        response = self.client.get(
            "/api/company-state/snapshot/?schema_version=1&org_slug=shizuha",
            **self.INGRESS,
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertEqual(response["Content-Type"].split(";")[0], "application/json")

    def test_internal_route_remains_masked_from_ingress(self):
        """PLS-383 guard invariant preserved: /api/internal/* via ingress is a
        masked 404 even for an authenticated staff session."""
        self.client.force_login(self.staff)
        response = self.client.get(
            "/api/internal/company-state/snapshot/?schema_version=1&org_slug=shizuha",
            **self.INGRESS,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": "Not found"})

    def test_internal_route_still_serves_in_cluster(self):
        """Backward compatibility: pod-to-pod calls (no forwarding headers)
        keep using the internal path."""
        self.client.force_login(self.staff)
        resolve, build = self._mock_service()
        with resolve, build:
            response = self.client.get(
                "/api/internal/company-state/snapshot/?schema_version=1&org_slug=shizuha",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["schema_version"], "company_state.snapshot.v1")


def _finance_payload(org_id=1, **overrides):
    """Build a valid books.finance_summary.v1 payload for tests."""
    now = timezone.now()
    payload = {
        "schema_version": "books.finance_summary.v1",
        "organization_id": org_id,
        "company_id": "00000000-0000-0000-0000-000000000001",
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "accounting_timezone": "Asia/Kolkata",
        "period": {
            "kind": "mtd",
            "from_date": now.date().replace(day=1).isoformat(),
            "to_date": now.date().isoformat(),
            "bounds": "inclusive",
        },
        "currency": "INR",
        "amounts": {
            "accounting_income": "1000.0000",
            "accounting_expenses": "400.0000",
            "accounting_net": "600.0000",
            "cash_and_bank": "5000.0000",
            "receivables": "200.0000",
            "payables": "300.0000",
        },
        "last_financial_activity_at": None,
        "semantics": {
            "accounting_income": "signed posted statutory income movements plus closing stock",
            "accounting_expenses": "signed posted statutory expense movements plus opening stock",
            "accounting_net": "accounting_income minus accounting_expenses",
            "negative_values": "allowed for net reversals, refunds, and natural-balance overdrafts",
        },
    }
    payload.update(overrides)
    return payload


class CompanyStateBooksFinanceTests(SimpleTestCase):
    """PLAT-5899 / PLAT-6004 spend_proxies Books adapter tests."""

    def setUp(self):
        cache.clear()
        BOOKS_LKG.clear()

    def _service(self, org_id=1, subject="user-1", claims=None, bearer="tok-1"):
        service = CompanyStateSnapshotService(
            org_slug="shizuha",
            bearer_token=bearer,
            subject=subject,
            claims=claims or {
                "iss": "shizuha-id",
                "aud": ["shizuha-pulse", "books"],
                "user_id": "user-1",
                "exp": int(time.time()) + 3600,
                "org_memberships": {"1": "admin"},
                "scope": "pulse:company-state.read",
            },
        )
        service.org = SnapshotOrg(id=org_id, slug="shizuha", name="Shizuha Global Pvt. Ltd")
        return service

    def test_valid_200_exposes_finance_and_seeds_lkg(self):
        service = self._service()
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload()),
        ) as get:
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "ok")
        finance = section["data"]["finance"]
        self.assertEqual(finance["schema_version"], "books.finance_summary.v1")
        self.assertEqual(finance["amounts"]["accounting_net"], "600.0000")
        self.assertEqual(section["data"]["venture_attribution"]["state"], "not_configured")
        # The exact current Bearer + numeric org id are forwarded every time.
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer tok-1")
        self.assertEqual(get.call_args.kwargs["headers"]["X-Organization-ID"], "1")
        self.assertIsNotNone(BOOKS_LKG.get(service._books_lkg_key()))

    def test_transient_failure_serves_stale_lkg(self):
        service = self._service()
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload()),
        ):
            service._spend_proxies_section()  # seeds LKG
        with patch(
            "tasks.services.company_state.requests.get",
            side_effect=requests.Timeout("deadline"),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "stale")
        self.assertEqual(section["data"]["finance"]["amounts"]["accounting_net"], "600.0000")
        self.assertEqual(section["errors"][0]["source"], "books")

    def test_transient_failure_without_lkg_is_unavailable(self):
        service = self._service()
        with patch(
            "tasks.services.company_state.requests.get",
            side_effect=requests.Timeout("deadline"),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "unavailable")
        self.assertIsNone(section["data"])

    def test_401_purges_lkg_and_returns_unavailable(self):
        service = self._service()
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload()),
        ):
            service._spend_proxies_section()  # seeds LKG
        self.assertIsNotNone(BOOKS_LKG.get(service._books_lkg_key()))
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response({"code": "source_authorization_denied"}, status=403),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "unavailable")
        self.assertEqual(section["errors"][0]["message"], "Books denied the forwarded caller authorization (401/403)")
        self.assertIsNone(BOOKS_LKG.get(service._books_lkg_key()))

    def test_subject_isolation_no_cross_principal_lkg(self):
        service_a = self._service(subject="user-a", bearer="tok-a")
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload()),
        ):
            service_a._spend_proxies_section()  # seeds LKG for user-a
        # A different subject with the same org must NOT reuse user-a's LKG.
        service_b = self._service(subject="user-b", bearer="tok-b")
        with patch(
            "tasks.services.company_state.requests.get",
            side_effect=requests.Timeout("deadline"),
        ):
            section = service_b._spend_proxies_section()
        self.assertEqual(section["state"], "unavailable")
        self.assertIsNone(section["data"])

    def test_org_isolation_no_cross_org_lkg(self):
        service_a = self._service(org_id=1, subject="user-1")
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload(org_id=1)),
        ):
            service_a._spend_proxies_section()  # seeds LKG for org 1
        service_b = self._service(org_id=2, subject="user-1")
        with patch(
            "tasks.services.company_state.requests.get",
            side_effect=requests.Timeout("deadline"),
        ):
            section = service_b._spend_proxies_section()
        self.assertEqual(section["state"], "unavailable")
        self.assertIsNone(section["data"])

    def test_org_mismatch_is_invalid_contract(self):
        service = self._service(org_id=1)
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload(org_id=2)),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIn("organization_id", section["errors"][0]["message"])
        self.assertIsNone(section["data"])
        # Invalid contract must never seed LKG.
        self.assertIsNone(BOOKS_LKG.get(service._books_lkg_key()))

    def test_schema_mismatch_is_invalid_contract(self):
        service = self._service()
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload(schema_version="books.finance_summary.v0")),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIn("schema", section["errors"][0]["message"])

    def test_malformed_decimal_is_invalid_contract(self):
        service = self._service()
        amounts = _finance_payload()["amounts"]
        amounts["accounting_income"] = "1e3"
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload(amounts=amounts)),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIn("accounting_income", section["errors"][0]["message"])
        self.assertIsNone(section["data"])

    def test_nan_decimal_is_invalid_contract(self):
        service = self._service()
        amounts = _finance_payload()["amounts"]
        amounts["accounting_expenses"] = "NaN"
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload(amounts=amounts)),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIsNone(section["data"])

    def test_negative_zero_is_invalid_contract(self):
        service = self._service()
        amounts = _finance_payload()["amounts"]
        amounts["accounting_income"] = "-0.0000"
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload(amounts=amounts)),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIsNone(section["data"])

    def test_bad_scale_decimal_is_invalid_contract(self):
        service = self._service()
        amounts = _finance_payload()["amounts"]
        amounts["accounting_net"] = "600.0"
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload(amounts=amounts)),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIsNone(section["data"])

    def test_net_invariant_mismatch_is_invalid_contract(self):
        service = self._service()
        amounts = _finance_payload()["amounts"]
        amounts["accounting_net"] = "999.0000"  # != 1000 - 400
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload(amounts=amounts)),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIn("accounting_net", section["errors"][0]["message"])
        self.assertIsNone(section["data"])

    def test_invalid_currency_is_invalid_contract(self):
        service = self._service()
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload(currency="usd")),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIn("currency", section["errors"][0]["message"])

    def test_missing_timezone_is_invalid_contract(self):
        service = self._service()
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload(accounting_timezone="")),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIn("accounting_timezone", section["errors"][0]["message"])

    def test_future_generated_at_is_invalid_contract(self):
        service = self._service()
        future = (timezone.now() + timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload(generated_at=future)),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIn("future", section["errors"][0]["message"])
        self.assertIsNone(section["data"])
        # Invalid contract never seeds LKG.
        self.assertIsNone(BOOKS_LKG.get(service._books_lkg_key()))

    def test_old_generated_at_is_invalid_contract(self):
        service = self._service()
        old = (timezone.now() - timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload(generated_at=old)),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIn("old", section["errors"][0]["message"])
        self.assertIsNone(section["data"])
        self.assertIsNone(BOOKS_LKG.get(service._books_lkg_key()))

    def test_404_company_not_configured(self):
        service = self._service()
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response({"code": "company_not_configured", "organization_id": 1}, status=404),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "not_configured")
        self.assertEqual(section["data"]["source_availability"], "source_dark")
        self.assertIsNone(section["data"]["finance"])
        self.assertNotEqual(section["errors"][0]["related_task"], "VEN-99")

    def test_409_config_required(self):
        service = self._service()
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response({"code": "accounting_timezone_not_configured"}, status=409),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "not_configured")
        self.assertIsNone(section["data"]["finance"])

    def test_400_invalid_request_is_degraded(self):
        service = self._service()
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response({"code": "invalid_accounting_period"}, status=400),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "degraded")
        self.assertIsNone(section["data"])

    def test_no_bearer_refused(self):
        service = self._service(bearer=None)
        section = service._spend_proxies_section()
        self.assertEqual(section["state"], "unavailable")
        self.assertIsNone(section["data"])

    def test_errors_never_contain_token_or_amounts(self):
        service = self._service(bearer="super-secret-bearer")
        with patch(
            "tasks.services.company_state.requests.get",
            side_effect=requests.Timeout("deadline"),
        ):
            section = service._spend_proxies_section()
        rendered = json.dumps(section)
        self.assertNotIn("super-secret-bearer", rendered)
        self.assertNotIn("1000.0000", rendered)
        self.assertNotIn("600.0000", rendered)

    def test_lkg_bounded_lru(self):
        # Fill past the bound with distinct subjects; only the most recent
        # entries survive and the oldest are evicted.
        for idx in range(BOOKS_LKG_MAX_ENTRIES + 10):
            service = self._service(subject=f"user-{idx}", bearer=f"tok-{idx}")
            with patch(
                "tasks.services.company_state.requests.get",
                return_value=_json_response(_finance_payload()),
            ):
                service._spend_proxies_section()
        self.assertLessEqual(len(BOOKS_LKG._entries), BOOKS_LKG_MAX_ENTRIES)
        # The oldest subject was evicted; a recent one is present.
        oldest = self._service(subject="user-0", bearer="tok-0")
        recent = self._service(subject=f"user-{BOOKS_LKG_MAX_ENTRIES + 9}", bearer="tok-x")
        self.assertIsNone(BOOKS_LKG.get(oldest._books_lkg_key()))
        self.assertIsNotNone(BOOKS_LKG.get(recent._books_lkg_key()))

    def test_lkg_cleared_on_process_restart_semantics(self):
        service = self._service()
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload()),
        ):
            service._spend_proxies_section()
        self.assertIsNotNone(BOOKS_LKG.get(service._books_lkg_key()))
        BOOKS_LKG.clear()  # process restart
        with patch(
            "tasks.services.company_state.requests.get",
            side_effect=requests.Timeout("deadline"),
        ):
            section = service._spend_proxies_section()
        self.assertEqual(section["state"], "unavailable")
        self.assertIsNone(section["data"])

    def test_stale_deadline_respects_token_expiry(self):
        # A token that expires in 60s caps the stale window at 60s, not 900s.
        service = self._service(claims={
            "iss": "shizuha-id",
            "aud": ["shizuha-pulse", "books"],
            "user_id": "user-1",
            "exp": int(time.time()) + 60,
            "org_memberships": {"1": "admin"},
            "scope": "pulse:company-state.read",
        })
        with patch(
            "tasks.services.company_state.requests.get",
            return_value=_json_response(_finance_payload()),
        ):
            service._spend_proxies_section()
        entry = BOOKS_LKG.get(service._books_lkg_key())
        self.assertIsNotNone(entry)
        self.assertLessEqual(
            entry["stale_deadline_mono"] - entry["receipt_mono"],
            60,
        )


class CompanyStateTerminalOwnerSuppressionTests(TestCase):
    """PLAT-5899: terminal related_task owners never stay active in missing_inputs."""

    def setUp(self):
        cache.clear()

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_terminal_related_task_is_suppressed_from_missing_inputs(self, org):
        org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")
        project = Project.objects.create(org_id=1, key="VEN", name="Venture")
        Item.objects.create(item_key="VEN-99", title="Terminal audit task", status="completed", project=project)
        Item.objects.create(item_key="PLAT-5899", title="Active carrier", status="in_progress", project=project)

        sections = {
            "spend_proxies": _section(
                state="not_configured",
                data=None,
            ),
            "learning_loop": _section(state="ok"),
        }
        sections["spend_proxies"]["errors"] = [
            {
                "source": "books",
                "message": "not configured",
                "required_action": "configure it",
                "owner_group": "accounting",
                "related_task": "VEN-99",
            },
            {
                "source": "books",
                "message": "carrier",
                "required_action": "carry the adapter",
                "owner_group": "engineering",
                "related_task": "PLAT-5899",
            },
        ]
        service = CompanyStateSnapshotService(org_slug="shizuha")
        missing = service._missing_inputs(sections)
        related = [entry["related_task"] for entry in missing]
        self.assertNotIn("VEN-99", related)
        self.assertIn("PLAT-5899", related)

    @patch.object(CompanyStateSnapshotService, "_resolve_org")
    def test_active_related_task_is_kept(self, org):
        org.return_value = SnapshotOrg(id=1, slug="shizuha", name="Shizuha Global Pvt. Ltd")
        project = Project.objects.create(org_id=1, key="PLAT", name="Platform")
        Item.objects.create(item_key="PLAT-5899", title="Active carrier", status="in_progress", project=project)
        sections = {
            "spend_proxies": _section(state="not_configured", data=None),
        }
        sections["spend_proxies"]["errors"] = [{
            "source": "books",
            "message": "carrier",
            "required_action": "carry the adapter",
            "owner_group": "engineering",
            "related_task": "PLAT-5899",
        }]
        service = CompanyStateSnapshotService(org_slug="shizuha")
        missing = service._missing_inputs(sections)
        self.assertEqual([entry["related_task"] for entry in missing], ["PLAT-5899"])
