"""PLAT-13196: verification-only terminal path for autonomous-bug.

Live gap found on PLAT-7175 (2026-11-08): the autonomous-bug workflow's only
Done lane is the PLS-493 repair gate ("Accept audited already-on-main repair")
whose seven server-resolved repair-evidence fields mandate provenance of a
repair. Cards whose outcome is verification-only (work verified
already-present/conformant — RBAC Role+Binding present since 2026-09-12,
7/7 can-i, zero drift) cannot honestly reach any terminal state: the repair
payload would fabricate provenance, Cancel/Discard is forbidden for completed
work, and Defer/Open misrepresent completion.

This migration adds an evidence-gated verification-only close to
autonomous-bug, mirroring the simple workflow's Resolved Elsewhere precedent
and the focused-qa typed-evidence validator style:

  1. Transitions ``todo -> done``, ``in_progress -> done`` and
     ``in_review -> done`` named "Verified Present — Close" whose validators
     REQUIRE a comment carrying the verification evidence (what was checked,
     the pre-existing state observed, and the verifier identity/receipt).
     The executing identity is server-resolved from the transition call, so
     verifier identity cannot be fabricated by screen data.
  2. Target is ``done`` (category done — truly terminal) rather than the
     ``resolved_elsewhere`` status (category todo), so a verification-closed
     card does not re-enter ready queues.

Not a bypass of PLS-493: the repair gate keeps its own transition and payload
contract; this is a distinct lane whose evidence asserts pre-existing state,
never repair provenance. Runs after the deploy-verify close gate
(regardless-of-workflow) — verification-only cards are non-governed/no-PR by
construction, so the code-mode pass applies.

Idempotent + reversible (mirrors the 0148 ``_upsert_transition`` pattern).
"""
from django.db import migrations

WORKFLOW_SLUG = 'autonomous-bug'
TRANSITION_NAME = 'Verified Present — Close'
FROM_SLUGS = ('todo', 'in_progress', 'in_review')

ARTICLE = {
    'type': 'inline',
    'title': 'Verified Present — Close (PLAT-13196)',
    'content': (
        'Verification-only terminal close: the work was checked and found '
        'ALREADY present/conformant on the live system — no repair was made, '
        'no PR exists, and none is implied. The closing comment MUST carry '
        'the verification evidence: exactly what was checked, the pre-existing '
        'state observed (receipts, can-i output, config read), and the '
        'verifier identity. This lane asserts pre-existing state; it NEVER '
        'asserts repair provenance — a card that produced a repair must close '
        'through the PLS-493 audited-repair lane instead. Misusing this lane '
        'for repair work is the provenance-fabrication class the repair '
        'contract exists to prevent.'
    ),
}

EVIDENCE_VALIDATOR = {
    'type': 'field_required',
    'field': 'comment',
    'message': (
        'Verified Present close requires verification evidence in the comment: '
        'what was checked, the pre-existing state observed, and the verifier '
        'identity/receipt (PLAT-13196).'
    ),
}


def _upsert_transition(WorkflowTransition, workflow, name, from_status, to_status, **defaults):
    transition = WorkflowTransition.objects.filter(
        workflow=workflow,
        name=name,
        from_status=from_status,
        to_status=to_status,
    ).first()
    if transition is None:
        WorkflowTransition.objects.create(
            workflow=workflow,
            name=name,
            from_status=from_status,
            to_status=to_status,
            **defaults,
        )
        return
    for field, value in defaults.items():
        setattr(transition, field, value)
    transition.save(update_fields=list(defaults.keys()))


def apply_verified_present_close(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    workflow = Workflow.objects.filter(slug=WORKFLOW_SLUG).first()
    if workflow is None:
        return
    status_slugs = set(
        workflow.workflow_statuses.values_list('status__slug', flat=True)
    )
    position = 40
    for from_slug in FROM_SLUGS:
        if from_slug not in status_slugs or 'done' not in status_slugs:
            continue
        _upsert_transition(
            WorkflowTransition,
            workflow,
            TRANSITION_NAME,
            from_slug,
            'done',
            description=(
                'Verification-only terminal close (PLAT-13196): work checked '
                'and found already present/conformant on the live system — '
                'no repair, no PR, none implied. Comment must carry the '
                'verification evidence and verifier identity. Repair work '
                'closes via the PLS-493 audited-repair lane, never here.'
            ),
            position=position,
            button_style='success',
            knowledge_articles=[ARTICLE],
            validators=[EVIDENCE_VALIDATOR],
            post_functions=[
                {
                    'type': 'send_notification',
                    'template': 'status_changed',
                    'recipients': ['assignee', 'reporter', 'watchers'],
                },
            ],
        )
        position += 1


def remove_verified_present_close(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    workflow = Workflow.objects.filter(slug=WORKFLOW_SLUG).first()
    if workflow is None:
        return
    WorkflowTransition.objects.filter(
        workflow=workflow,
        name=TRANSITION_NAME,
        to_status='done',
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0203_merge_escalation_receipt_plat9355_branch'),
    ]

    operations = [
        migrations.RunPython(
            apply_verified_present_close,
            remove_verified_present_close,
        ),
    ]
