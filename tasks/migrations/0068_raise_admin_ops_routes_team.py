"""Route Raise to Admin Ops through the Admin Ops team, not direct operator assignment."""
from django.db import migrations


OLD_BODIES = {
    (
        '⚠️ Raised to Admin Ops (operator) — blocked, needs human/operator action. '
        'Was in: {{from_status}}.'
    ),
    'Raised to Admin Ops (operator action needed).',
}
NEW_BODY = (
    '⚠️ Raised to Admin Ops — blocked for Admin Ops triage. Was in: '
    '{{from_status}}. Admin Ops must route credential/infra gaps to DevOps/Hive '
    'and create an explicit human blocker only for genuine operator-only decisions.'
)


def route_admin_ops_to_team(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for transition in WorkflowTransition.objects.filter(name='Raise to Admin Ops'):
        changed = False
        post_functions = []
        for pf in transition.post_functions or []:
            pf = dict(pf)
            if pf.get('type') == 'assign_to' and pf.get('target') == 'operator':
                pf = {'type': 'assign_team', 'value': 'admin-ops'}
                changed = True
            elif pf.get('type') == 'add_comment' and pf.get('body') in OLD_BODIES:
                pf['body'] = NEW_BODY
                changed = True
            post_functions.append(pf)
        if changed:
            transition.post_functions = post_functions
            transition.save(update_fields=['post_functions'])


def restore_operator_route(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for transition in WorkflowTransition.objects.filter(name='Raise to Admin Ops'):
        changed = False
        post_functions = []
        for pf in transition.post_functions or []:
            pf = dict(pf)
            if pf.get('type') == 'assign_team' and pf.get('value') == 'admin-ops':
                pf = {'type': 'assign_to', 'target': 'operator'}
                changed = True
            elif pf.get('type') == 'add_comment' and pf.get('body') == NEW_BODY:
                pf['body'] = next(iter(OLD_BODIES))
                changed = True
            post_functions.append(pf)
        if changed:
            transition.post_functions = post_functions
            transition.save(update_fields=['post_functions'])


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0067_autonomous_bug_approve_close_no_pr'),
    ]
    operations = [
        migrations.RunPython(route_admin_ops_to_team, restore_operator_route),
    ]
