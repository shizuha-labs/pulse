"""PLAT-11284: disposition-consistent live-boundary evidence gate on security-finding rejects.

Migration 0163 (PLAT-6192) added the `live_boundary_evidence` validator to
transitions named iexact 'Reject' — which covers the needs_triage-stage Reject
(path A, id c102e019…) but NOT the found-stage `Verification Complete - Reject`
(path B). Result: the same terminal disposition (rejected) evaluates the
content-safety gate on one path and skips it entirely on the other — a
reject-as-fixed verdict that fails the evidence bar on path A sails through
path B (the exact DOJO-40 class the gate exists to stop), while seats whose
payloads do not satisfy the same-comment heading+surface+persist conjunction
read the denial as an opaque seat wedge (observed 2026-10-05: ren 0-for-5 on A
vs 4-for-4 on B, akira 6-for-6 on A).

This migration extends the validator to the found-stage reject transition (and
any other security-finding transition whose to_status is the same rejected
terminal status), making gate coverage disposition-based rather than
name-based. Purely additive on t.validators; rollback removes by type.
"""
from django.db import migrations

LIVE_BOUNDARY_MESSAGE = (
    'Reject-as-fixed denied (PLAT-6192): post a `## Live-boundary evidence` '
    'comment showing the fix through the REAL caller sequence (API/CLI/browser '
    'command + URL) AND asserting the persisted layer (DB row / session history / '
    'API re-read) is safe and equal to the rendered output. Helper/unit-level '
    'proof alone is denied.'
)

# Found-stage reject transition observed permissive on 2026-10-05 (ren 4-for-4
# on payloads that path A denied 0-for-5).
FOUND_STAGE_REJECT_NAME = 'Verification Complete - Reject'


def _add_validator(transition):
    validators = list(transition.validators or [])
    if any(v.get('type') == 'live_boundary_evidence' for v in validators):
        return False
    validators.append({'type': 'live_boundary_evidence', 'message': LIVE_BOUNDARY_MESSAGE})
    transition.validators = validators
    transition.save(update_fields=['validators'])
    return True


def add_found_stage_reject_validator(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    updated = 0
    # 1. The named found-stage reject transition.
    for t in WorkflowTransition.objects.filter(name__iexact=FOUND_STAGE_REJECT_NAME):
        if _add_validator(t):
            updated += 1
    if updated:
        return
    # 2. Fallback: any other transition in the SAME workflow that lands on the
    #    rejected terminal status of the gated path-A Reject
    #    (c102e019-abf9-44d8-b4fb-b202b32ef223). Match to_status only.
    #    Filtering from_status too (the needs_triage slug) misses a renamed
    #    found-stage reject, which leaves a different stage (rui on pulse#818).
    gated = WorkflowTransition.objects.filter(
        id='c102e019-abf9-44d8-b4fb-b202b32ef223'
    ).first()
    if gated is None or not gated.workflow_id:
        return
    for t in WorkflowTransition.objects.filter(
        workflow_id=gated.workflow_id,
        to_status=gated.to_status,
    ).exclude(id=gated.id):
        if _add_validator(t):
            updated += 1


def remove_found_stage_reject_validator(apps, schema_editor):
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    for t in WorkflowTransition.objects.filter(name__iexact=FOUND_STAGE_REJECT_NAME):
        t.validators = [v for v in (t.validators or [])
                        if v.get('type') != 'live_boundary_evidence']
        t.save(update_fields=['validators'])


class Migration(migrations.Migration):

    dependencies = [
        # PLAT-11284: re-parent onto the current leaf. Depending on 0190 left
        # a second leaf beside 0201_plat10787 (premerge run 9047).
        ('tasks', '0201_plat10787_attestation_provider_key'),
    ]

    operations = [
        migrations.RunPython(add_found_stage_reject_validator, remove_found_stage_reject_validator),
    ]
