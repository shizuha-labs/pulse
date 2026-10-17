"""PLS-425: route feature-request-rfc 'Needs Info' to the owning team, not admin-ops.

The 'Needs Info' (from rfc_triage/design/approved/implementation_linked/validation)
and 'Request More Info' (from rfc_intake) transitions were seeded with
post_functions=[{'type': 'assign_team', 'value': 'admin-ops'}] (0072). That
admin-ops routing makes WorkflowService._is_operator_bound_escalation_transition()
classify them as operator-bound escalations, so the transition runs through the
PLAT-1347 escalation-routing gate; on any umbrella RFC the PLAT-3136 hygiene gate
flags `umbrella_has_children` and _reroute_operator_escalation() overrides the
intended move — persisting rfc_intake and reassigning the team to `security`, while
reporting success (corrupted HIVE-624 mid-review, 2026-07-11).

'Needs Info' is a normal wait-for-info state; the RFC must stay with its owning
Architecture team (matching the sibling 'Info Provided' transition). Re-point every
rfc_needs_info-target transition's assign_team from admin-ops to architecture so it
never enters the operator path.
"""
from django.db import migrations


def fix_rfc_needs_info_routing(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    workflow = Workflow.objects.filter(slug='feature-request-rfc').first()
    if workflow is None:
        return

    for transition in WorkflowTransition.objects.filter(
        workflow=workflow, to_status='rfc_needs_info',
    ):
        post_functions = transition.post_functions or []
        changed = False
        for pf in post_functions:
            if (
                isinstance(pf, dict)
                and pf.get('type') == 'assign_team'
                and pf.get('value') == 'admin-ops'
            ):
                pf['value'] = 'architecture'
                changed = True
        if changed:
            transition.post_functions = post_functions
            transition.save(update_fields=['post_functions'])


def noop_reverse(apps, schema_editor):
    # Reverting would restore the broken admin-ops routing (the corruption path);
    # make the reverse an explicit no-op rather than silently reintroducing it.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0091_pls106_workflow_stale_timer'),
    ]

    operations = [
        migrations.RunPython(fix_rfc_needs_info_routing, noop_reverse),
    ]
