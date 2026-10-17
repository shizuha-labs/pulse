"""PLAT-9681 rule 2 — fleet-wide reopen edge from every terminal status.

The ruling (sora, PLAT-9681 cmt 840411): ``reopen`` valid from ALL terminal
states fleet-wide — done must not be absorbing. Migration 0185's forward
function ensures a ``Reopen`` edge from every terminal status each active
workflow actually contains, targeting the workflow's own recovery stage,
idempotently, and WITHOUT rewriting bespoke live customization (an existing
Reopen edge from a terminal stage is never touched, whatever its target).

Tests invoke the migration's forward/reverse functions directly (the real
app registry + a schema editor) rather than driving MigrationExecutor: the
edge idempotency under test lives in the forward function's identity logic,
and executor record-table bookkeeping (applied-migration rows, keepdb state)
is orthogonal machinery that made a double executor run order-dependent
under CI's persistent test database (run 7152 finding).
"""

import importlib

from django.apps import apps as global_apps
from django.db import connection
from django.test import TestCase

from tasks.models import Status, Workflow, WorkflowTransition

plat9681 = importlib.import_module(
    "tasks.migrations.0185_plat9681_fleetwide_terminal_reopen")


class FleetwideTerminalReopenTests(TestCase):
    def _make_status(self, slug, name=None):
        status, _ = Status.objects.get_or_create(
            slug=slug, defaults={"name": name or slug.replace("_", " ").title()},
        )
        return status

    def _make_workflow(self, slug, status_slugs, initial="todo", active=True):
        statuses = {s: self._make_status(s) for s in status_slugs}
        workflow = Workflow.objects.create(
            name="wf-" + slug, slug=slug, is_active=active,
            initial_status=statuses[initial],
        )
        for position, s in enumerate(status_slugs):
            workflow.workflow_statuses.create(status=statuses[s], position=position)
        return workflow

    def _forwards(self):
        plat9681.ensure_fleetwide_terminal_reopen(
            global_apps, connection.schema_editor())

    def _reverse(self):
        plat9681.remove_fleetwide_terminal_reopen(
            global_apps, connection.schema_editor())

    def reopen_edges(self, workflow):
        return {
            (t.from_status, t.to_status)
            for t in WorkflowTransition.objects.filter(
                workflow=workflow, name="Reopen")
        }

    def test_reopen_edge_created_for_every_terminal_status(self):
        workflow = self._make_workflow(
            "drifted-a",
            ["todo", "in_progress", "in_review", "awaiting_merge",
             "done", "cancelled", "deferred", "resolved", "reopened"],
        )
        # 0023-class seed coverage: only done has a reopen edge today.
        WorkflowTransition.objects.create(
            workflow=workflow, name="Reopen", from_status="done", to_status="reopened",
        )
        pre_count = WorkflowTransition.objects.filter(workflow=workflow).count()

        self._forwards()

        edges = self.reopen_edges(workflow)
        for terminal in ("done", "cancelled", "deferred", "resolved"):
            self.assertIn((terminal, "reopened"), edges)
        # Non-terminal stages get no edge; the bespoke seeded edge is untouched.
        self.assertNotIn(("in_progress", "reopened"), edges)
        self.assertNotIn(("in_review", "reopened"), edges)
        self.assertEqual(
            WorkflowTransition.objects.filter(workflow=workflow).count(),
            pre_count + 3,  # cancelled + deferred + resolved created
        )

    def test_bespoke_reopen_edge_is_never_rewritten(self):
        workflow = self._make_workflow(
            "drifted-b", ["todo", "in_progress", "done", "cancelled", "reopened"],
        )
        WorkflowTransition.objects.create(
            workflow=workflow, name="Reopen", from_status="done",
            to_status="todo",  # bespoke operator target
        )
        self._forwards()
        done_targets = list(
            WorkflowTransition.objects.filter(
                workflow=workflow, name="Reopen", from_status="done",
            ).values_list("to_status", flat=True)
        )
        self.assertEqual(done_targets, ["todo"])  # untouched, not duplicated

    def test_idempotent_rerun_creates_no_duplicates(self):
        workflow = self._make_workflow(
            "drifted-c", ["todo", "in_progress", "done", "reopened"],
        )
        self._forwards()
        first = self.reopen_edges(workflow)
        self._forwards()
        second = self.reopen_edges(workflow)
        self.assertEqual(first, second)
        self.assertEqual(first, {("done", "reopened")})

    def test_workflow_without_recovery_stage_targets_initial_status(self):
        # No reopened/todo/open stage at all — the initial status is the target.
        workflow = self._make_workflow(
            "drifted-d", ["in_progress", "in_review", "done"],
            initial="in_progress",
        )
        self._forwards()
        self.assertIn(("done", "in_progress"), self.reopen_edges(workflow))

    def test_inactive_workflows_are_left_alone(self):
        workflow = self._make_workflow(
            "drifted-e", ["in_progress", "done"], initial="in_progress",
            active=False,
        )
        self._forwards()
        self.assertEqual(self.reopen_edges(workflow), set())

    def test_reverse_deletes_only_prefix_marked_edges(self):
        workflow = self._make_workflow(
            "drifted-f", ["todo", "in_progress", "done", "cancelled", "reopened"],
        )
        bespoke = WorkflowTransition.objects.create(
            workflow=workflow, name="Reopen", from_status="done",
            to_status="todo", description="operator bespoke edge",
        )
        self._forwards()
        # Bespoke suppresses done; the migration creates the prefix-marked
        # cancelled edge. Both edges present before reverse.
        self.assertGreaterEqual(
            self.reopen_edges(workflow),
            {("done", "todo"), ("cancelled", "reopened")},
        )
        self._reverse()
        remaining = self.reopen_edges(workflow)
        # The prefix-marked created edge is gone; the bespoke edge survives.
        self.assertNotIn(("cancelled", "reopened"), remaining)
        self.assertIn(("done", "todo"), remaining)
        self.assertTrue(
            WorkflowTransition.objects.filter(pk=bespoke.pk).exists()
        )
