"""PLAT-9681 rule 2 — fleet-wide reopen edge from EVERY terminal status.

Ruling (sora, cmt 840411): ``reopen`` valid from ALL terminal states fleet-
wide — done must not be absorbing. The 0023 seed gave ``Reopen`` edges only
from ``done``/``closed`` on the workflows it created; live workflows drifted
since, and the other terminal slugs (``cancelled``/``deferred``/``resolved``/
``merged``/``rejected``/``duplicate``/``wont_fix``/``expired``/``failed``/
``silenced``/...) have no recovery edge on most workflows. The PLAT-9100
correction died as a comment on a ``done`` task precisely because the reopen
edge was absent where the mistake was discovered.

This migration ensures, for every ACTIVE workflow, a ``Reopen`` transition
from EVERY terminal status the workflow actually contains, targeting the
workflow's own recovery stage (``reopened`` if present, else ``todo``/``open``,
else the initial status). Idempotent (get_or_create); existing edges —
including the seeded done/closed ones and any bespoke targets — are untouched.

Discipline note (deliberate, recorded here so reviewers don't flag it): the
edge existing does NOT license agents to reopen ``deferred``/``cancelled``
work on their own — the Deferred/Cancel articles keep their agent-side hard
rules. The edge is the correction instrument for erroneous terminalization
(the Cancel article itself says cancel is the wrong path for fixable work;
this edge is how that mistake is repaired), and its execution authority
follows each workflow's role conditions.
"""

from django.db import migrations

from ..services.workflow_service import WorkflowService

_REOPEN_TARGET_PREFERENCE = ("reopened", "todo", "open")


def _reopen_target_slug(workflow, status_slugs):
    slug_set = set(status_slugs)
    for candidate in _REOPEN_TARGET_PREFERENCE:
        if candidate in slug_set:
            return candidate
    initial = (workflow.initial_status.slug if workflow.initial_status_id else "") or ""
    return initial or None


def ensure_fleetwide_terminal_reopen(apps, schema_editor):
    Workflow = apps.get_model("tasks", "Workflow")
    WorkflowTransition = apps.get_model("tasks", "WorkflowTransition")

    for workflow in Workflow.objects.filter(is_active=True).iterator():
        status_slugs = list(
            workflow.workflow_statuses.order_by("position")
            .values_list("status__slug", flat=True)
        )
        target = _reopen_target_slug(workflow, status_slugs)
        if not target:
            continue
        for slug in status_slugs:
            normalized = (slug or "").strip().lower().replace("-", "_")
            if not WorkflowService._status_is_terminal(normalized):
                continue
            if normalized == target:
                # A terminal slug that is itself the reopen target (e.g. a
                # workflow whose recovery stage is "open" while "open" is
                # terminal there) cannot self-reopen; skip fail-safe.
                continue
            existing = WorkflowTransition.objects.filter(
                workflow=workflow,
                name="Reopen",
                from_status=slug,
            ).exists()
            if existing:
                # A bespoke Reopen edge from this terminal stage already
                # exists (any target). Ruling grants the EDGE, not a specific
                # target — never rewrite live operator customization.
                continue
            WorkflowTransition.objects.create(
                workflow=workflow,
                name="Reopen",
                from_status=slug,
                to_status=target,
                description=(
                    "PLAT-9681 rule 2: fleet-wide recovery edge — reopen from "
                    "terminal states so done is not absorbing. The edge's "
                    "execution authority follows this workflow's role "
                    "conditions; agent-side Deferred/Cancel discipline "
                    "articles are unchanged."
                ),
                position=90,
            )


def remove_fleetwide_terminal_reopen(apps, schema_editor):
    """Reverse only edges this migration created (marker: description prefix)."""
    WorkflowTransition = apps.get_model("tasks", "WorkflowTransition")
    WorkflowTransition.objects.filter(
        name="Reopen",
        description__startswith="PLAT-9681 rule 2:",
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("tasks", "0184_plat9491_scope_assertions"),
    ]

    operations = [
        migrations.RunPython(
            ensure_fleetwide_terminal_reopen,
            remove_fleetwide_terminal_reopen,
        ),
    ]
