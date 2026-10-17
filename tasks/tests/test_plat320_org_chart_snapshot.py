"""PLAT-320 — OrgChartSnapshotView._aggregate_workload regression coverage.

Covers the workload-aggregation review findings:
- bucketing: a configured status not in the hard-coded fine-grained list but
  carrying a coarse category (e.g. `in_qa` → category `in_progress`) buckets by
  category, not falling through to `open`.
- org scope: counts are scoped to the snapshot org (project__org_id), not every org.
- done-category exclusion: a status whose category is `done` (even if its slug
  isn't in the hard-coded terminal set) is excluded from active workload.
- assignee identity: a valid task assignment is also counted in the response's
  user-id workload map.
- group canonicalization: an assignment_group spelled `admin_ops` is keyed as
  `admin-ops` so it matches the team lookup.
"""
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Item, Project, ProjectMembership, Status
from tasks.views import OrgChartSnapshotView
from .base import create_blocked_item, satisfy_blocked_invariant

ADMIN_BY_SLUG = "tasks.services.admin_service.admin_service.get_organization_by_slug"


class Plat320WorkloadAggregationTests(TestCase):
    def setUp(self):
        # This suite exercises snapshot aggregation, not assignment
        # authorization. Establish its historical rows through an explicitly
        # authorized writer while leaving the database fence active.
        assignment_authorization = patch(
            "tasks.services.workflow_service.WorkflowService.assignment_org_mismatch",
            return_value=(False, "fixture-org", "fixture-org"),
        )
        assignment_authorization.start()
        self.addCleanup(assignment_authorization.stop)

        # Self-contained: don't rely on the migration seed.
        Status.objects.get_or_create(slug="in_qa", defaults={"name": "In QA", "category": "in_progress"})
        Status.objects.get_or_create(slug="open", defaults={"name": "Open", "category": "todo"})
        # A done-category status whose slug is NOT in _TERMINAL_STATUSES.
        Status.objects.get_or_create(slug="shipped", defaults={"name": "Shipped", "category": "done"})
        # A scheduled-category status (recurring/future park) — must also be excluded.
        Status.objects.get_or_create(slug="snoozed", defaults={"name": "Snoozed", "category": "scheduled"})

        self.org_a = Project.objects.create(name="A", key="AAA", org_id=1, access_level="open")
        self.org_b = Project.objects.create(name="B", key="BBB", org_id=2, access_level="open")

        def mk(project, status, email=None, group=None, assignee_id=None):
            factory = create_blocked_item if status == 'blocked' else Item.objects.create
            item = factory(
                # Unseeded workflow name → Item.save applies no status coercion, so
                # these fine-grained statuses (in_qa/shipped/snoozed/…) survive for
                # the snapshot's global-category bucketing instead of being remapped
                # to the 'simple' workflow's slugs.
                mode="task", title="t", project=project, status=status,
                workflow_name="autonomous-dev",
                assignee_email=email, assignment_group=group, assignee_id=assignee_id,
                created_by_email="sys@shizuha.com",
            )
            if status == 'blocked':
                satisfy_blocked_invariant(item)

        # org A
        mk(self.org_a, "in_qa", email="alice@shizuha.com")     # bucketing -> in_progress
        mk(self.org_a, "open", email="alice@shizuha.com")      # -> open
        mk(self.org_a, "blocked", email="alice@shizuha.com")   # -> blocked (fine-grained wins over category)
        mk(self.org_a, "in_qa", group="engineering")           # team bucket -> in_progress
        # done-category task — must be EXCLUDED from active workload
        mk(self.org_a, "shipped", email="carol@shizuha.com")
        mk(self.org_a, "open", email="carol@shizuha.com")      # carol's one active task
        # scheduled-category task — must also be EXCLUDED
        mk(self.org_a, "snoozed", email="dave@shizuha.com")
        mk(self.org_a, "in_qa", email="dave@shizuha.com")      # dave's one active task
        # Valid direct-human assignment — must also be counted by user id.
        ProjectMembership.objects.create(
            project=self.org_a,
            user_id=777,
            user_email="legacy777@shizuha.com",
            is_active=True,
        )
        mk(
            self.org_a,
            "in_progress",
            assignee_id=777,
            email="legacy777@shizuha.com",
        )
        # group spelled with an underscore — must canonicalize to 'admin-ops'
        mk(self.org_a, "in_progress", group="admin_ops")
        # Same assignee with work in two groups — personal totals should include
        # both, but team-scoped card totals must not bleed across groups.
        mk(self.org_a, "open", email="eve@shizuha.com", group="admin_ops")
        mk(self.org_a, "blocked", email="eve@shizuha.com", group="program-management")
        # org B — must be excluded when scoped to org A
        mk(self.org_b, "in_progress", email="bob@shizuha.com")
        mk(self.org_b, "open", group="engineering")
        # HIVE-2627: same assignee + same group slug in a SECOND org — the bare
        # assignee-team key is the cross-org sum; only the org-qualified key
        # may feed a single org's team card.
        mk(self.org_b, "open", email="eve@shizuha.com", group="admin_ops")

    def _run(self, org_id):
        with patch(ADMIN_BY_SLUG, return_value=({"id": org_id} if org_id else None)):
            return OrgChartSnapshotView()._aggregate_workload()

    def test_configured_in_progress_status_buckets_in_progress_not_open(self):
        by_email, _, _, _, _ = self._run(org_id=1)
        a = by_email["alice@shizuha.com"]
        self.assertEqual(a["in_progress"], 1)  # the in_qa task
        self.assertEqual(a["open"], 1)
        self.assertEqual(a["blocked"], 1)      # blocked keeps its own bucket despite category in_progress

    def test_counts_scoped_to_snapshot_org(self):
        by_email, by_group, _, _, _ = self._run(org_id=1)
        # bob's tasks live in org B → excluded entirely
        self.assertNotIn("bob@shizuha.com", by_email)
        # engineering's in_qa is org A only; org B's open engineering task is excluded
        self.assertEqual(by_group["engineering"]["in_progress"], 1)
        self.assertEqual(by_group["engineering"]["open"], 0)

    def test_waiting_star_statuses_are_blocked_not_open(self):
        """waiting_* holds are todo-category but awareness-only on owner queues.

        Hive treated snapshot open>0 as assigned_tasks and kept DeepSeek leases
        for agents whose heartbeat was queue_empty (hiro/rei 2026-08-21:
        waiting_external counted as open). Use waiting_customer here so the
        PLS-733 typed-admission trigger is not in the way of the bucket test.
        """
        Status.objects.get_or_create(
            slug="waiting_customer",
            defaults={"name": "Waiting Customer", "category": "todo"},
        )
        item = Item.objects.create(
            mode="task", title="waiting hold", project=self.org_a,
            status="open", workflow_name="autonomous-dev",
            assignee_email="rei@shizuha.com", assignment_group="review",
            created_by_email="sys@shizuha.com",
        )
        Item.objects.filter(pk=item.pk).update(status="waiting_customer")
        by_email, _, _, _, _ = self._run(org_id=1)
        row = by_email["rei@shizuha.com"]
        self.assertEqual(row["blocked"], 1, row)
        self.assertEqual(row["open"], 0, row)

    def test_done_category_status_excluded_from_workload(self):
        by_email, _, _, _, _ = self._run(org_id=1)
        # carol's 'shipped' (category done) task is excluded; only her 'open' counts.
        c = by_email["carol@shizuha.com"]
        self.assertEqual(c["open"], 1)
        self.assertEqual(c["in_progress"], 0)
        # The done-category task is excluded from ACTIVE workload; it is tracked
        # separately in the 'completed' meta-bucket (not open/in_progress/blocked/
        # in_review), so the active-workload total is 1.
        self.assertEqual(c["completed"], 1)
        self.assertEqual(
            c["open"] + c["in_progress"] + c["blocked"] + c["in_review"], 1)

    def test_scheduled_category_status_counts_into_scheduled_bucket(self):
        # HIVE-609: scheduled/parked work is NOT terminal — Hive team cards render
        # a dedicated 'scheduled' column, which was structurally zero while these
        # statuses were excluded. They now count into their own bucket, keeping
        # 'active' math (open/in_progress) unchanged.
        by_email, _, _, _, _ = self._run(org_id=1)
        d = by_email["dave@shizuha.com"]
        self.assertEqual(d["in_progress"], 1)   # the in_qa task
        self.assertEqual(d["scheduled"], 1)     # the snoozed task, own bucket
        self.assertEqual(sum(d.values()), 2)

    def test_org_scope_all_fans_out_across_orgs(self):
        # HIVE-609: org_scope='all' aggregates every org's items (no admin lookup).
        view = OrgChartSnapshotView()
        by_email, by_group, _, _, _ = view._aggregate_workload(org_scope='all')
        self.assertEqual(by_email["bob@shizuha.com"]["in_progress"], 1)  # org B item
        self.assertEqual(by_email["alice@shizuha.com"]["open"], 1)       # org A item
        # engineering has work in BOTH orgs: in_qa (A) + open (B)
        self.assertEqual(by_group["engineering"]["in_progress"], 1)
        self.assertEqual(by_group["engineering"]["open"], 1)

    def test_assignee_task_counted_by_id(self):
        _, _, by_uid, _, _ = self._run(org_id=1)
        # The task assigned to id 777 is counted, keyed by str(id).
        self.assertIn("777", by_uid)
        self.assertEqual(by_uid["777"]["in_progress"], 1)

    def test_assignment_group_canonicalized(self):
        _, by_group, _, _, _ = self._run(org_id=1)
        # 'admin_ops' is stored canonicalized so it matches the 'admin-ops' team.
        self.assertIn("admin-ops", by_group)
        self.assertEqual(by_group["admin-ops"]["in_progress"], 1)
        self.assertNotIn("admin_ops", by_group)

    def test_assignee_team_workload_is_scoped_by_assignment_group(self):
        by_email, _, _, by_assignee_team, _ = self._run(org_id=1)
        self.assertEqual(by_email["eve@shizuha.com"]["open"], 1)
        self.assertEqual(by_email["eve@shizuha.com"]["blocked"], 1)
        self.assertEqual(by_assignee_team["eve@shizuha.com::admin-ops"]["open"], 1)
        self.assertEqual(by_assignee_team["eve@shizuha.com::admin-ops"]["blocked"], 0)
        self.assertEqual(by_assignee_team["eve@shizuha.com::program-management"]["open"], 0)
        self.assertEqual(by_assignee_team["eve@shizuha.com::program-management"]["blocked"], 1)

    def test_assignee_team_workload_org_qualified_in_fan_out_mode(self):
        # HIVE-2627: in 'all' fan-out mode the bare `email::group` key is the
        # cross-org SUM (eve has admin-ops work in org 1 AND org 2). The
        # org-qualified keys must carry each org's own counts so one org's
        # team card never paints another org's WIP.
        _, _, _, by_assignee_team, _ = self._run(org_id='all')
        self.assertEqual(by_assignee_team["eve@shizuha.com::admin-ops"]["open"], 2)
        self.assertEqual(by_assignee_team["eve@shizuha.com::org1::admin-ops"]["open"], 1)
        self.assertEqual(by_assignee_team["eve@shizuha.com::org2::admin-ops"]["open"], 1)
        # Org-scoped snapshots keep the same qualified keys for their own org.
        _, _, _, scoped_team, _ = self._run(org_id=2)
        self.assertEqual(scoped_team["eve@shizuha.com::org2::admin-ops"]["open"], 1)
        self.assertNotIn("eve@shizuha.com::org1::admin-ops", scoped_team)

    def test_fail_open_when_org_unresolved(self):
        # org_id unresolved → unscoped (both orgs counted) so the snapshot still renders
        by_email, _, _, _, _ = self._run(org_id=None)
        self.assertIn("alice@shizuha.com", by_email)
        self.assertIn("bob@shizuha.com", by_email)

    def test_snapshot_view_exposes_required_helper_methods(self):
        view = OrgChartSnapshotView()
        self.assertTrue(callable(view._get_availability_map))
        self.assertTrue(callable(view._aggregate_workload))
        self.assertTrue(callable(view._aggregate_alert_workload))

    def test_assigned_firing_alerts_are_in_snapshot(self):
        Item.objects.create(
            mode='alert', title='Origin CI failed', project=self.org_a,
            status='firing', source='origin-ci',
            assignee_email='kai@agents.shizuha.io',
            assignment_group='engineering',
        )
        Item.objects.create(
            mode='alert', title='unassigned ops', project=self.org_a,
            status='firing', source='pulse-ops-anomaly',
            assignment_group='devops',
        )
        counts = OrgChartSnapshotView()._aggregate_alert_workload(org_scope='all')
        self.assertEqual(counts.get('kai@agents.shizuha.io'), 1)
        self.assertEqual(counts.get('kai@shizuha.com'), 1)
        self.assertNotIn('', counts)

    def test_agent_alias_email_folds_into_canonical_hive_card_keys(self):
        """Hive Agents cards look up ryo@shizuha.com::merge.

        Pulse may store ryo@agents.shizuha.io (identity-service spelling).
        WIP and get_my_tasks already fold PLAT-533 aliases; org-chart
        aggregation did not, so a loaded merger rendered as an empty card.
        """
        Status.objects.get_or_create(
            slug="awaiting_merge",
            defaults={"name": "Awaiting Merge", "category": "in_review"},
        )
        alias_item = Item.objects.create(
            mode="task", title="alias merge work", project=self.org_a,
            status="open", workflow_name="autonomous-dev",
            assignee_email="ryo@agents.shizuha.io", assignment_group="merge",
            created_by_email="sys@shizuha.com",
        )
        Item.objects.filter(pk=alias_item.pk).update(status="awaiting_merge")
        Item.objects.create(
            mode="task", title="canonical merge work", project=self.org_a,
            status="open", workflow_name="autonomous-dev",
            assignee_email="ryo@shizuha.com", assignment_group="merge",
            created_by_email="sys@shizuha.com",
        )
        by_email, _, _, by_assignee_team, _ = self._run(org_id=1)
        self.assertNotIn("ryo@agents.shizuha.io", by_email)
        self.assertEqual(by_email["ryo@shizuha.com"]["in_review"], 1)
        self.assertEqual(by_email["ryo@shizuha.com"]["open"], 1)
        self.assertNotIn("ryo@agents.shizuha.io::merge", by_assignee_team)
        self.assertEqual(by_assignee_team["ryo@shizuha.com::merge"]["in_review"], 1)
        self.assertEqual(by_assignee_team["ryo@shizuha.com::merge"]["open"], 1)

    def test_awaiting_deploy_is_in_review_not_open(self):
        """Hive 2026-09-15: awaiting_deploy fell through to open, so merge
        seats (kei) got GLM assigned_tasks while heartbeats saw 0 ready."""
        Status.objects.get_or_create(
            slug="awaiting_deploy",
            defaults={"name": "Awaiting Deploy", "category": "in_progress"},
        )
        Item.objects.create(
            mode="task", title="waiting on ci", project=self.org_a,
            status="awaiting_deploy", workflow_name="autonomous-bug",
            assignee_email="kei@shizuha.com", assignment_group="merge",
            created_by_email="sys@shizuha.com",
        )
        by_email, by_group, _, by_assignee_team, _ = self._run(org_id=1)
        self.assertEqual(by_email["kei@shizuha.com"]["in_review"], 1)
        self.assertEqual(by_email["kei@shizuha.com"]["open"], 0)
        self.assertEqual(by_group["merge"]["in_review"], 1)
        self.assertEqual(by_assignee_team["kei@shizuha.com::merge"]["in_review"], 1)

    def test_unassigned_by_group_counts_only_queue_items(self):
        # Single-team invariant (operator 2026-07-11): team cards = member-sum
        # + unassigned queue. The queue map counts ONLY unassigned items.
        _, by_group, _, _, unassigned = self._run(org_id=1)
        # org A engineering has exactly one item and it is unassigned (queue).
        self.assertEqual(unassigned['engineering']['in_progress'], 1)
        # admin-ops: the unassigned in_progress item is queue; eve's assigned
        # 'open' item is NOT.
        self.assertEqual(unassigned['admin-ops']['in_progress'], 1)
        self.assertEqual(unassigned['admin-ops']['open'], 0)
        # group totals still include assigned work.
        self.assertEqual(by_group['admin-ops']['open'], 1)


class RosterDrivenTeamListTests(TestCase):
    """Operator directive 2026-07-14: teams and tasks live in Pulse — the
    Admin catalog must never GATE which teams exist in the snapshot. TeamRoster
    teams are unioned in even when admin returns nothing (outage, pagination,
    missing row), and duplicate admin catalog rows collapse to one entry."""

    def _snapshot(self, admin_teams, org_scope="shizuha"):
        from django.contrib.auth import get_user_model
        from django.test import RequestFactory

        staff = get_user_model().objects.create_user(
            username="snapstaff", email="snapstaff@shizuha.com",
            password="x", is_staff=True,
        )
        req = RequestFactory().get(
            "/api/admin/org-chart/snapshot/", {"organization_slug": org_scope}
        )
        req.user = staff
        with patch.object(OrgChartSnapshotView, "_fetch_admin_teams", return_value=admin_teams), \
             patch.object(OrgChartSnapshotView, "_get_availability_map", return_value={}), \
             patch(ADMIN_BY_SLUG, return_value=None):
            return OrgChartSnapshotView.as_view()(req).data

    def test_roster_team_survives_admin_returning_nothing(self):
        from tasks.models import TeamRoster
        # A seeded 'qa' roster row may already exist in the test DB — assert on
        # the canonical row rather than racing the unique constraint.
        roster, _ = TeamRoster.objects.update_or_create(
            team_name="qa", organization_slug="shizuha",
            defaults={"tiers": [["zen@shizuha.com"]], "is_active": True},
        )
        data = self._snapshot(admin_teams=[])
        names = {t["name"].lower() for t in data["teams"]}
        self.assertIn("qa", names)
        qa_members = [n for n in data["nodes"] if n["email"] == "zen@shizuha.com"]
        self.assertEqual(len(qa_members), 1)

    def test_duplicate_admin_rows_collapse_to_one_team(self):
        dup = [
            {"id": 7, "name": "Review", "organization_slug": "shizuha", "members": []},
            {"id": 31, "name": "Review", "organization_slug": "shizuha", "members": []},
        ]
        data = self._snapshot(admin_teams=dup)
        review_rows = [t for t in data["teams"] if t["name"].lower() == "review"]
        self.assertEqual(len(review_rows), 1)
        self.assertEqual(str(review_rows[0]["id"]), "7")

    def test_admin_display_alias_uses_canonical_team_workload(self):
        from tasks.models import TeamRoster

        # Historical test seeds may still store the input alias `research`;
        # update directly so this snapshot test exercises read canonicalization
        # without turning model-save alias cleanup into part of its fixture.
        updated = TeamRoster.objects.filter(
            team_name="research", organization_slug="shizuha",
        ).update(tiers=[["hana@shizuha.com"]], is_active=True)
        if not updated:
            updated = TeamRoster.objects.filter(
                team_name="research-analytics", organization_slug="shizuha",
            ).update(tiers=[["hana@shizuha.com"]], is_active=True)
        if not updated:
            TeamRoster.objects.bulk_create([TeamRoster(
                team_name="research",
                organization_slug="shizuha",
                tiers=[["hana@shizuha.com"]],
                is_active=True,
            )])
            updated = 1
        self.assertEqual(updated, 1)
        empty = {
            "open": 0, "in_progress": 0, "blocked": 0, "in_review": 0,
            "scheduled": 0, "backlog": 0, "completed": 0, "overdue": 0,
        }
        member_work = {**empty, "blocked": 1, "scheduled": 15}
        queue_work = {**empty, "open": 2}
        aggregate = (
            {},
            {},
            {},
            {"hana@shizuha.com::research-analytics": member_work},
            {"research-analytics": queue_work},
        )

        with patch.object(OrgChartSnapshotView, "_aggregate_workload", return_value=aggregate):
            data = self._snapshot(admin_teams=[{
                "id": 30,
                "name": "Research",
                "organization_slug": "shizuha",
                "members": [],
            }])

        self.assertEqual(data["team_workload"]["30"]["open"], 2)
        self.assertEqual(data["team_workload"]["30"]["blocked"], 1)
        self.assertEqual(data["team_workload"]["30"]["scheduled"], 15)

    def test_admin_only_team_still_included(self):
        # HR-side teams that exist only in admin (no routing roster) keep rendering.
        data = self._snapshot(admin_teams=[
            {"id": 40, "name": "Governance", "organization_slug": "shizuha", "members": []},
        ])
        names = {t["name"].lower() for t in data["teams"]}
        self.assertIn("governance", names)

    def test_same_named_admin_teams_do_not_copy_members_across_orgs(self):
        from tasks.models import TeamRoster

        TeamRoster.objects.update_or_create(
            team_name="accounting", organization_slug="shizuha",
            defaults={"tiers": [["banto@shizuha.com"]], "is_active": True},
        )
        admin_member = {"email": "banto@shizuha.com"}
        data = self._snapshot(admin_teams=[
            {
                "id": 1, "name": "Accounting", "organization_slug": "shizuha",
                "members": [admin_member],
            },
            {
                "id": 26, "name": "Accounting", "organization_slug": "shizuha-digital",
                "members": [admin_member],
            },
        ], org_scope="all")

        by_org = {team["organization_slug"]: team for team in data["teams"]
                  if team["name"].lower() == "accounting"}
        self.assertEqual(by_org["shizuha"]["member_ids"], ["email:banto@shizuha.com"])
        self.assertEqual(by_org["shizuha-digital"]["member_ids"], [])

    def test_admin_only_lead_is_not_a_second_agent_membership(self):
        from tasks.models import TeamRoster

        TeamRoster.objects.update_or_create(
            team_name="architecture", organization_slug="shizuha",
            defaults={"tiers": [["sora@shizuha.com"]], "is_active": True},
        )
        data = self._snapshot(admin_teams=[
            {
                "id": 9, "name": "Architecture", "organization_slug": "shizuha",
                "lead_email": "sora@shizuha.com", "members": [],
            },
            {
                "id": 42, "name": "Cluster Managers", "organization_slug": "shizuha",
                "lead_email": "sora@shizuha.com", "members": [],
            },
        ])

        teams = {team["name"]: team for team in data["teams"]}
        self.assertEqual(teams["Architecture"]["member_ids"], ["email:sora@shizuha.com"])
        self.assertEqual(teams["Architecture"]["lead_email"], "sora@shizuha.com")
        self.assertEqual(teams["Cluster Managers"]["member_ids"], [])
        self.assertIsNone(teams["Cluster Managers"]["lead_email"])
