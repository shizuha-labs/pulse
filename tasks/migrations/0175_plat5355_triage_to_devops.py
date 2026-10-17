"""PLAT-5355: add 'Triage to DevOps' to autonomous-bug (needs_triage → open).

The autonomous-bug workflow has four triage-out transitions (Triage to
Architecture / Engineering / QA / Security) but no path to DevOps, so an
infra-owned bug (Longhorn, k3s, CI runners, ingress, node health, cluster
networking) can only reach `open` by firing a wrong-team triage transition and
then correcting the group with a second `pulse_assign_task` call — the exact
both-at-once move `pulse-core` forbids, leaving a misattributed audit trail and
a window where the item sits on the wrong queue.

This adds one purely additive transition mirroring the existing four exactly:
from_status=needs_triage, to_status=open, assign_team=devops, with the same
three knowledge articles the sibling triage transitions carry. `Escalate to
DevOps` (from_status=* → needs_triage) is intentionally left unchanged — it
serves a different purpose (pull an item back into triage).

Idempotent + reversible.
"""
from django.db import migrations

_NAME = 'Triage to DevOps'
_KA = [
    {
        'type': 'inline',
        'title': 'Triage the bug',
        'content': (
            "Read the repro steps. If the bug is obviously not a bug (user error, "
            "expected behavior), set resolution=wont_fix via Reject. Otherwise tag "
            "the task with the affected component and pick a Start Progress "
            "transition — that routes the task to the right pod. No human triage "
            "needed."
        ),
    },
    {
        'type': 'inline',
        'title': 'Bug Triage = Information Gathering FIRST',
        'content': (
            "Before triaging, GATHER: (1) wiki_search_pages on the symptom/component "
            "- prior incidents and standards often already explain it; (2) read the "
            "FULL ticket + every linked/related ticket (pulse_get_issue_links) + "
            "search Pulse for the same error text (pulse_search_tasks) - recurrences "
            "change the routing; (3) record what you found as a comment (sources "
            "checked, prior occurrences, suspected layer). A bug that RECURS after a "
            "claimed fix, affects 2+ agents/teams, or breached SLA routes to "
            "ARCHITECTURE for contract triage, not straight to engineering (lesson: "
            "PLS-3 - tool schema said str, docs said JSON array; symptom-level fixes "
            "missed the contract bug for days)."
        ),
    },
    {
        'type': 'inline',
        'title': 'Containment first (8D-D3)',
        'content': (
            "If the bug is actively burning capacity or affecting users, apply an "
            "INTERIM CONTAINMENT before the root-cause work starts (disable the path, "
            "hot-patch, workaround flag) and record it on the ticket - containment is "
            "not the fix and must be listed for removal at closure. (PLS-3 "
            "containment: loop-guard + VLLM_STREAM_WITH_TOOLS=0; CTX-26 containment: "
            "coordinator-repaired playwright-runner.)"
        ),
    },
]
_PF = [
    {'type': 'assign_team', 'value': 'devops'},
    {'type': 'send_notification', 'template': 'status_changed',
     'recipients': ['assignee', 'watchers']},
]


def add_triage_to_devops(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    wf = Workflow.objects.filter(slug='autonomous-bug').first()
    if not wf:
        return
    status_slugs = set(wf.workflow_statuses.values_list('status__slug', flat=True))
    if 'needs_triage' not in status_slugs or 'open' not in status_slugs:
        return
    WorkflowTransition.objects.get_or_create(
        workflow=wf,
        name=_NAME,
        from_status='needs_triage',
        to_status='open',
        defaults={
            'description': (
                'Triage complete; route an infra-owned bug to the DevOps pod. Use for '
                'Longhorn/storage, k3s scheduling, Forgejo/CI runners, ingress, node '
                'health, and cluster networking bugs that belong to DevOps rather than '
                'Engineering/Architecture/QA/Security.'
            ),
            'position': 6,
            'button_style': 'default',
            'post_functions': _PF,
            'knowledge_articles': _KA,
        },
    )


def remove_triage_to_devops(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    wf = Workflow.objects.filter(slug='autonomous-bug').first()
    if wf:
        WorkflowTransition.objects.filter(
            workflow=wf, name=_NAME, from_status='needs_triage', to_status='open',
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0174_merge_0173_leaves'),
    ]
    operations = [
        migrations.RunPython(add_triage_to_devops, remove_triage_to_devops),
    ]
