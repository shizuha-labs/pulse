"""Prefer team-based workflow routing over concrete-person assignment.

Workflow transitions should route work to a team/pod wherever possible. Pulse
then resolves the current available concrete assignee from that team. This keeps
handoffs resilient when one backend/provider is unavailable.
"""
import os

from django.db import migrations


def apply_team_assignment_invariants(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    # Approved PRs route to the merge team. Merge-team members hold kai2574
    # credentials and escalate to Admin Ops only when repo/access/settings block
    # the merge.
    for t in WorkflowTransition.objects.filter(
        workflow__slug='software-development',
        name='Approve Review',
        from_status='in_review',
        to_status='awaiting_merge',
    ):
        pfs = [
            pf for pf in (t.post_functions or [])
            if not (
                pf.get('type') == 'assign_to'
                and pf.get('target') == 'user'
                and os.environ.get('PULSE_OPERATOR_EMAIL', '').strip()
                and (pf.get('user_email') or '').lower() == os.environ['PULSE_OPERATOR_EMAIL'].strip().lower()
            )
        ]
        pfs = [pf for pf in pfs if pf.get('type') != 'assign_team']
        if not any(pf.get('type') == 'route_merge' for pf in pfs):
            pfs.append({'type': 'route_merge'})
        t.post_functions = pfs
        t.save(update_fields=['post_functions'])

    # Fill in legacy ambiguous assign_team post-functions. A blank assign_team
    # relies on pre-existing assignment_group and can silently weaken routing.
    explicit_team_edges = {
        ('change-request', 'Request Changes', 'in_review', 'proposed'): 'engineering',
        ('change-request', 'Submit for Review', 'proposed', 'in_review'): 'review',
        ('incident', 'Begin Postmortem', 'under_review', 'documenting'): 'documentation',
        ('incident', 'Reopen', 'under_review', 'in_progress'): 'engineering',
        ('incident', 'Resume from Escalation', 'escalated', 'in_progress'): 'engineering',
        ('incident', 'Start Investigation', 'open', 'in_progress'): 'engineering',
        ('incident', 'Escalate to Leads', '*', 'escalated'): 'admin-ops',
        ('workflow-change', 'Submit for Review', 'proposed', 'under_review'): 'admin-ops',
        ('security-finding', 'Auto-triage', 'found', 'needs_triage'): 'security',
        ('security-finding', 'Reopen', 'completed', 'reopened'): 'security',
    }
    for (slug, name, from_status, to_status), team in explicit_team_edges.items():
        for t in WorkflowTransition.objects.filter(
            workflow__slug=slug,
            name=name,
            from_status=from_status,
            to_status=to_status,
        ):
            changed = False
            pfs = []
            for pf in (t.post_functions or []):
                pf = dict(pf)
                if pf.get('type') == 'assign_team' and (
                    not (pf.get('team') or pf.get('value'))
                    or (pf.get('team') or pf.get('value')) in ('leads', 'security-triage')
                ):
                    pf.pop('team', None)
                    pf['value'] = team
                    changed = True
                pfs.append(pf)
            if changed:
                t.post_functions = pfs
                t.save(update_fields=['post_functions'])

    for t in WorkflowTransition.objects.filter(
        workflow__slug='security-finding',
        name='Approve Review',
        from_status='in_review',
        to_status='awaiting_merge',
    ):
        pfs = [pf for pf in (t.post_functions or []) if pf.get('type') != 'assign_team']
        if not any(pf.get('type') == 'route_merge' for pf in pfs):
            pfs.append({'type': 'route_merge'})
        t.post_functions = pfs
        t.save(update_fields=['post_functions'])

    for t in WorkflowTransition.objects.filter(to_status='awaiting_merge'):
        changed = False
        pfs = []
        for pf in (t.post_functions or []):
            pf = dict(pf)
            if pf.get('type') == 'add_comment' and 'operator merge' in (pf.get('body') or '').lower():
                pf['body'] = (
                    'Approved — awaiting merge-team landing of the linked PR. '
                    'Merge team uses kai2574 merge access and escalates to Admin Ops '
                    'only if repo settings, credentials, or policy block the merge.'
                )
                changed = True
            pfs.append(pf)
        if changed:
            t.post_functions = pfs
            t.save(update_fields=['post_functions'])

    WorkflowTransition.objects.filter(
        workflow__slug='autonomous-bug',
        name='Verified — Hand to Operator for Merge',
        from_status='verification',
        to_status='awaiting_merge',
    ).update(name='Verified — Hand to Merge Team')


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0043_stash_implementer_all_review_edges'),
    ]

    operations = [
        migrations.RunPython(apply_team_assignment_invariants, noop),
    ]
