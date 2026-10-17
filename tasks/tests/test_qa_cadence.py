"""PLAT-552 — qa_verification_sweep CVQA-L2 cadence task.

Covers: cadence/schedule-firing logic (T1/T2/T3/T4 due-day rules), per-project
task creation (workflow/team/priority + project resolution incl. the platform
fallback), and deduplication (no duplicate while one is open; regenerates once
the prior one is terminal).
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from tasks import qa_cadence
from tasks.models import Item, Project
from tasks.qa_target_map import MapFetch, MapRow
from tasks.tasks import qa_verification_sweep


# Concrete reference dates (UTC-naive is fine; only weekday/week/day are read):
#   2026-01-05 Mon, ISO week 2 (even)   -> T1,T2,T3 due; T4 not
#   2026-01-12 Mon, ISO week 3 (odd)    -> T1,T2 due;    T3,T4 not
#   2026-01-06 Tue                      -> T1 only
#   2026-02-01 Sun, day==1              -> T1,T4 due;    T2,T3 not
MON_EVEN = datetime(2026, 1, 5, 6, 23)
MON_ODD = datetime(2026, 1, 12, 6, 23)
TUE = datetime(2026, 1, 6, 6, 23)
FIRST_OF_MONTH = datetime(2026, 2, 1, 6, 23)


class TierDueLogicTests(TestCase):
    """Pure cadence logic — which tiers fire on which day."""

    def test_t1_is_due_every_day(self):
        for d in (MON_EVEN, MON_ODD, TUE, FIRST_OF_MONTH):
            self.assertTrue(qa_cadence.tier_is_due("T1", d))

    def test_weekly_biweekly_on_mondays(self):
        self.assertEqual(qa_cadence.due_tiers(MON_EVEN), ["T1", "T2", "T3"])
        self.assertEqual(qa_cadence.due_tiers(MON_ODD), ["T1", "T2"])

    def test_non_monday_is_t1_only(self):
        self.assertEqual(qa_cadence.due_tiers(TUE), ["T1"])

    def test_monthly_on_the_first(self):
        self.assertIn("T4", qa_cadence.due_tiers(FIRST_OF_MONTH))
        self.assertNotIn("T4", qa_cadence.due_tiers(TUE))
        # Feb 1 2026 is a Sunday, so only T1 + T4.
        self.assertEqual(qa_cadence.due_tiers(FIRST_OF_MONTH), ["T1", "T4"])


class SweepCreationTests(TestCase):
    def setUp(self):
        # Owning projects for a few T1 services + the platform fallback.
        self.platform = Project.objects.create(org_id=1, name="Shizuha Platform", key="PLAT")
        self.connect = Project.objects.create(org_id=1, name="Shizuha Connect", key="CON")
        self.pulse = Project.objects.create(org_id=1, name="Shizuha Pulse", key="PLS")
        self.kura = Project.objects.create(org_id=1, name="Kura", key="KURA")
        self.dojo = Project.objects.create(org_id=1, name="Dojo", key="DOJO")
        # Hermetic wiki: fetch errors → static checklists, no map-refresh tasks.
        patcher = patch(
            "tasks.qa_cadence.fetch_target_map_full",
            return_value=MapFetch(error="test: wiki offline"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _t1(self):
        return qa_verification_sweep(force_tiers=["T1"])

    def test_creates_one_task_per_service_with_correct_fields(self):
        self._t1()
        tasks = Item.objects.filter(source=qa_cadence.QA_CADENCE_SOURCE)
        # T1 has 6 services (id-auth, connect, pulse, kura, platform-ui, dojo).
        self.assertEqual(tasks.count(), 6)
        for t in tasks:
            self.assertEqual(t.mode, "task")
            self.assertEqual(t.workflow_name, "autonomous-dev")
            self.assertEqual(t.assignment_group, "qa")
            self.assertEqual(t.priority, "low")
            self.assertIn("qa-cadence", t.labels)
            self.assertIn("[QA Cadence]", t.title)

    def test_project_resolution_and_platform_fallback(self):
        self._t1()
        connect_task = Item.objects.get(source_id="T1:connect")
        self.assertEqual(connect_task.project_id, self.connect.id)
        # id-auth and platform-ui have project_key PLAT -> the platform project.
        id_task = Item.objects.get(source_id="T1:id-auth")
        self.assertEqual(id_task.project_id, self.platform.id)
        self.assertIn("service:id-auth", id_task.labels)

    def test_dedup_no_duplicate_while_open(self):
        first = self._t1()
        self.assertIn("created=6", first)
        before = Item.objects.filter(source=qa_cadence.QA_CADENCE_SOURCE).count()
        second = self._t1()
        after = Item.objects.filter(source=qa_cadence.QA_CADENCE_SOURCE).count()
        self.assertEqual(before, after)  # no new tasks
        self.assertIn("created=0", second)
        self.assertIn("skipped=6", second)

    def test_regenerates_after_prior_task_is_terminal(self):
        self._t1()
        done = Item.objects.get(source_id="T1:connect")
        done.status = "done"
        done.save(update_fields=["status"])
        self._t1()
        # The connect cadence task regenerates (now 2 rows for it: 1 done, 1 open);
        # the other four stay deduped (still open).
        connect_rows = Item.objects.filter(source_id="T1:connect")
        self.assertEqual(connect_rows.count(), 2)
        self.assertEqual(connect_rows.exclude(status__in=qa_cadence.TERMINAL_STATUSES).count(), 1)
        self.assertEqual(Item.objects.filter(source_id="T1:pulse").count(), 1)

    def test_db_constraint_rejects_concurrent_active_duplicate(self):
        self._t1()
        existing = Item.objects.get(source_id="T1:connect")
        fields = qa_cadence.build_task_fields("T1", qa_cadence.QA_TIERS["T1"]["services"][1], self.connect, TUE)
        fields["item_key"] = self.connect.get_next_item_key()

        # On Postgres a failed statement aborts the whole transaction; inside a
        # TestCase's outer atomic, the expected IntegrityError must be contained in
        # its own savepoint or the subsequent existing.save() runs on a broken
        # connection.
        from django.db import transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            Item.objects.create(**fields)

        existing.status = "done"
        existing.save(update_fields=["status"])
        fields["item_key"] = self.connect.get_next_item_key()
        replacement = Item.objects.create(**fields)
        self.assertEqual(replacement.source_id, "T1:connect")

    def test_unresolved_project_does_not_crash(self):
        # Drop the platform fallback: services whose key project is absent and
        # have no platform fallback are reported, not created — never crash.
        self.platform.delete()
        summary = self._t1()
        # connect/pulse/kura/dojo still resolve to their own projects (4 created);
        # id-auth + platform-ui (PLAT, now missing) land in unresolved_project.
        self.assertIn("created=4", summary)
        self.assertIn("id-auth", summary)
        self.assertIn("platform-ui", summary)

    def test_pls923_fetch_error_fails_loud_on_cadence_task(self):
        """PLS-923 acceptance #4: a wiki fetch/auth error (the live 401) must
        surface the fetch-error banner on the generated cadence task — never a
        silent smoke-pass that hides an existing Target Map."""
        # The setUp patcher already makes every map fetch error; run a T1 sweep
        # and assert the generated task carries the fetch-error banner.
        self._t1()
        task = Item.objects.get(source_id="T1:connect")
        self.assertIn("NO TARGET MAP (fetch error)", task.description)
        self.assertIn("coverage is smoke-only", task.description)
        # And the static checklist is still embedded so the task is actionable.
        self.assertIn("### Checklist", task.description)


class RecurringDriverDedupTests(TestCase):
    """PLS-925: one cadence authority per (source, source_id).

    A recurring driver is the authority even while it sits in its
    terminal/scheduled gap; the sweep must NOT create a per-window sibling
    (PLAT-5169 production storm: sibling + driver's next fire collided on
    uniq_active_qa_cadence_source, 1,442 retries/minute). A terminal,
    non-recurring task from a prior window still does not block the next.
    """

    def setUp(self):
        self.platform = Project.objects.create(org_id=1, name="Shizuha Platform", key="PLAT")
        self.connect = Project.objects.create(org_id=1, name="Shizuha Connect", key="CON")
        patcher = patch(
            "tasks.qa_cadence.fetch_target_map_full",
            return_value=MapFetch(error="test: wiki offline"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_recurring_driver(self):
        fields = qa_cadence.build_task_fields(
            "T1", qa_cadence.QA_TIERS["T1"]["services"][1], self.connect, TUE
        )
        fields["item_key"] = self.connect.get_next_item_key()
        fields["is_recurring"] = True
        fields["frequency"] = "daily"
        fields["recurrence_pattern"] = '{"interval": 1, "unit": "day"}'
        return Item.objects.create(**fields)

    def test_terminal_recurring_driver_blocks_sibling_across_consecutive_sweeps(self):
        driver = self._make_recurring_driver()
        # Driver is in its terminal/scheduled gap (cycle closed for the period).
        driver.status = "done"
        driver.save(update_fields=["status"])

        # Two consecutive top-level sweeps: no sibling created, constraint never
        # hit — exactly one active generation (the driver) per (source, source_id).
        for _ in range(2):
            summary = qa_verification_sweep(force_tiers=["T1"])
            self.assertIn("created=0", summary)
        rows = Item.objects.filter(
            source=qa_cadence.QA_CADENCE_SOURCE, source_id="T1:connect"
        )
        self.assertEqual(rows.count(), 1)  # only the driver — no sibling
        # The unique constraint is satisfied: one active row.
        self.assertEqual(
            rows.exclude(status__in=qa_cadence.TERMINAL_STATUSES).count(), 0
        )

    def test_terminal_non_recurring_still_regenerates(self):
        # A terminal, NON-recurring task from a prior window does not block the
        # next window's task (existing behavior preserved).
        qa_verification_sweep(force_tiers=["T1"])
        done = Item.objects.get(source_id="T1:connect")
        done.status = "done"
        done.save(update_fields=["status"])
        qa_verification_sweep(force_tiers=["T1"])
        connect_rows = Item.objects.filter(source_id="T1:connect")
        self.assertEqual(connect_rows.count(), 2)
        self.assertEqual(
            connect_rows.exclude(status__in=qa_cadence.TERMINAL_STATUSES).count(), 1
        )


class MapMaintenanceTests(TestCase):
    """CVQA-L7 map maintenance (operator 2026-07-14): the sweep files a
    build/refresh task to qa for every registry service whose Target Map is
    missing, row-less, or stale — deduped, and never on wiki fetch errors."""

    def setUp(self):
        self.platform = Project.objects.create(org_id=1, name="Shizuha Platform", key="PLAT")
        self.connect = Project.objects.create(org_id=1, name="Shizuha Connect", key="CON")

    def _sweep_with(self, connect_fetch):
        """Run a T1 sweep where Connect's map fetch returns ``connect_fetch``
        and every other service errors (excluded from maintenance)."""
        def fake_fetch(map_title):
            if map_title == "QA Target Map — Connect":
                return connect_fetch
            return MapFetch(error="test: wiki offline")

        with patch("tasks.qa_cadence.fetch_target_map_full", side_effect=fake_fetch):
            return qa_verification_sweep(force_tiers=["T1"])

    def _refresh_tasks(self):
        return Item.objects.filter(
            source=qa_cadence.QA_CADENCE_SOURCE,
            source_id="map-refresh:connect",
        )

    def test_absent_map_files_refresh_task(self):
        self._sweep_with(MapFetch(exists=False))
        task = self._refresh_tasks().get()
        self.assertEqual(task.item_type, "qa.discovery")
        self.assertEqual(task.assignment_group, "qa")
        self.assertEqual(task.priority, "normal")
        self.assertIn("map-maintenance", task.labels)
        self.assertIn("no Target Map page exists", task.description)
        self.assertIn("QA Target Map — Connect", task.description)

    def test_rowless_map_files_refresh_task(self):
        self._sweep_with(MapFetch(exists=True, rows=[], updated_at=timezone.now()))
        task = self._refresh_tasks().get()
        self.assertIn("no parseable F0/F1/F2 rows", task.description)

    def test_stale_map_files_refresh_task(self):
        stale = timezone.now() - timedelta(days=qa_cadence.MAP_STALE_DAYS + 5)
        rows = [MapRow("F0", "Feed", "loads", "", "")]
        self._sweep_with(MapFetch(exists=True, rows=rows, updated_at=stale))
        task = self._refresh_tasks().get()
        self.assertIn("map stale", task.description)

    def test_fresh_map_files_nothing(self):
        rows = [MapRow("F0", "Feed", "loads", "", "")]
        self._sweep_with(MapFetch(
            exists=True, rows=rows, updated_at=timezone.now() - timedelta(days=1)
        ))
        self.assertEqual(self._refresh_tasks().count(), 0)

    def test_fetch_error_files_nothing(self):
        # A wiki outage must not mass-file refresh tasks.
        self._sweep_with(MapFetch(error="boom"))
        self.assertEqual(self._refresh_tasks().count(), 0)

    def test_refresh_task_dedup_while_open(self):
        self._sweep_with(MapFetch(exists=False))
        self._sweep_with(MapFetch(exists=False))
        self.assertEqual(self._refresh_tasks().count(), 1)

    def test_refresh_regenerates_after_terminal(self):
        self._sweep_with(MapFetch(exists=False))
        task = self._refresh_tasks().get()
        task.status = "done"
        task.save(update_fields=["status"])
        self._sweep_with(MapFetch(exists=False))
        self.assertEqual(self._refresh_tasks().count(), 2)


class RegistryIntegrityTests(TestCase):
    """The cadence registry must stay aligned with the canonical QA-space maps."""

    def test_slugs_unique_across_tiers(self):
        slugs = [s.slug for t in qa_cadence.QA_TIERS.values() for s in t["services"]]
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_all_map_titles_canonical_prefix(self):
        for svc in qa_cadence.all_services():
            self.assertTrue(
                svc.map_title.startswith("QA Target Map — "),
                f"{svc.slug}: bad map_title {svc.map_title!r}",
            )

    def test_known_title_divergences_are_explicit(self):
        """Services whose QA-space page is NOT named after the display name
        must carry an explicit map_title (the PLS-238 launch gap)."""
        by_slug = {s.slug: s for s in qa_cadence.all_services()}
        expected = {
            "connect": "QA Target Map — Connect",
            "pulse": "QA Target Map — Pulse",
            "id-auth": "QA Target Map — Shizuha ID",
            "scs": "QA Target Map — SCS",
            "wiki": "QA Target Map — Wiki",
            "drive": "QA Target Map — Drive",
            "cli": "QA Target Map — SCLI",
            "books": "QA Target Map — Books",
            "notes": "QA Target Map — Notes",
            "mail": "QA Target Map — Mail",
            "finance": "QA Target Map — Finance",
            "browser": "QA Target Map — Browser",
            "platform-ui": "QA Target Map — Shizuha Platform (cross-cutting infra)",
            "origin": "QA Target Map — Shizuha Origin (Forgejo dev platform)",
        }
        for slug, title in expected.items():
            self.assertIn(slug, by_slug, f"service {slug} missing from registry")
            self.assertEqual(by_slug[slug].map_title, title)

    def test_deployed_services_present(self):
        """Every deployed user-facing service has a recurring cadence entry
        (registry-completeness gap closed 2026-07-14; PLS-923 extends to
        Mail + Notes so the CVQA-L2/L7 registry is exhaustive)."""
        slugs = {s.slug for s in qa_cadence.all_services()}
        for required in (
            "search", "origin", "admin", "forge", "inventory", "dojo",
            "mail", "notes",
        ):
            self.assertIn(required, slugs)

    def test_pls923_forge_mail_notes_inventory_registered(self):
        """PLS-923: Forge/Mail/Notes/Inventory must be enumerated in the cadence
        registry (acceptance #1) with their canonical CVQA-L7 map titles."""
        by_slug = {s.slug: s for s in qa_cadence.all_services()}
        for slug, title in {
            "forge": "QA Target Map — Forge",
            "mail": "QA Target Map — Mail",
            "notes": "QA Target Map — Notes",
            "inventory": "QA Target Map — Inventory",
        }.items():
            self.assertIn(slug, by_slug, f"PLS-923: {slug} missing from registry")
            self.assertEqual(by_slug[slug].map_title, title)
            # Each must be filed to a resolvable owning project key (or the
            # platform fallback) so the sweep can create its cadence task.
            self.assertTrue(by_slug[slug].project_key)

    def test_pls923_existing_map_never_falls_back_to_smoke(self):
        """PLS-923 acceptance #2: a parseable existing map must compose F0+F1/F2
        rows — never a false NO TARGET MAP / smoke-only fallback."""
        rows = [
            MapRow("F0", "Feed", "loads", "", ""),
            MapRow("F1", "DM", "delivers", "", ""),
            MapRow("F2", "Search", "returns", "", ""),
        ]
        fetched = MapFetch(exists=True, rows=rows, page_url="https://wiki/x",
                           updated_at=timezone.now())
        svc = qa_cadence.QA_TIERS["T3"]["services"][0]
        checklist, map_used = qa_cadence._build_checklist_from_map("T3", svc, map_fetch=fetched)
        self.assertTrue(map_used)
        self.assertIn("F0", checklist)
        self.assertIn("F1", checklist)
        self.assertIn("F2", checklist)
        self.assertNotIn("NO TARGET MAP", checklist)
        self.assertNotIn("fetch error", checklist)
