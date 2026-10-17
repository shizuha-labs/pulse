"""PLS-425 (corrective): 'Needs Info' must PRESERVE the RFC's owning team.

Migration 0092 re-pointed every `feature-request-rfc` transition targeting
`rfc_needs_info` from `admin-ops` to the literal team `architecture`. That fixed
the operator-escalation misclassification (Architecture Info-Provided sibling),
but `feature-request-rfc` is NOT Architecture-exclusive — e.g. PLAT-4055 runs
this workflow QA-owned (`zen@shizuha.com`). A hard-coded `architecture` still
mis-routes: a QA-owned (or any non-Architecture) RFC taking Needs Info would be
silently reassigned to Architecture (hiro, Architecture P1 on PLS-425).

The correct wait-for-info contract is owner-PRESERVING: entering `rfc_needs_info`
must not reassign the team at all — the item keeps its current
`assignment_group`/`assignee`. The engine only mutates the team when a transition
carries an `assign_team` post-function (`execute_post_functions`, the
`pf_type == 'assign_team'` branch) and `rfc_needs_info` is not a status-lane-owned
status (`_STATUS_LANE_ASSIGNMENT_GROUPS` only owns `awaiting_merge`). So removing
the `assign_team` post-function from every `rfc_needs_info`-target transition
leaves the owning team untouched AND keeps the transition out of the
operator-escalation classifier (`_is_operator_bound_escalation_transition`, which
keys on `assign_team ∈ {admin-ops, human-ops, leads, ceo-decisions}`).

Idempotent and robust to DB state: strips the `assign_team` post-function whether
its current value is `admin-ops` (0092 not yet applied to a given DB) or
`architecture` (0092 applied); preserves any other post-functions.
"""
from django.db import migrations


_NEEDS_INFO_STATUS = 'rfc_needs_info'


def preserve_owning_team_on_needs_info(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    workflow = Workflow.objects.filter(slug='feature-request-rfc').first()
    if workflow is None:
        return

    for transition in WorkflowTransition.objects.filter(
        workflow=workflow, to_status=_NEEDS_INFO_STATUS,
    ):
        post_functions = transition.post_functions or []
        kept = [
            pf for pf in post_functions
            if not (isinstance(pf, dict) and pf.get('type') == 'assign_team')
        ]
        if len(kept) != len(post_functions):
            transition.post_functions = kept
            transition.save(update_fields=['post_functions'])


def noop_reverse(apps, schema_editor):
    # Reverting would reintroduce a hard-coded team on the wait-for-info
    # transition (either the admin-ops corruption path or the architecture
    # mis-route); make the reverse an explicit no-op.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0092_pls425_rfc_needs_info_owner_team'),
    ]

    operations = [
        migrations.RunPython(preserve_owning_team_on_needs_info, noop_reverse),
    ]
