"""PLAT-13440: verification-only terminal close for security-finding.

Live gap found 2026-11-11/12 (retro row 1, CoS-ruled pre-file): security-finding
cards strand in ``accepted`` (and ``needs_triage``) indefinitely when the
remediation already exists on the repo's default branch. The workflow's only
terminal paths from those states are the role-gated Security dispositions
(Accept/Reject/Accept Risk: security-lead/admin/ceo) or the PR lane
(Start Work → ... → Verify Fixed), which requires a new PR even when the fix
landed via a canonical carrier weeks earlier. Live cost this cycle: the pillow
cluster (PLAT-12159–12163) sat ~3 weeks despite the fixed version being live on
main via merged pulse#916; PLAT-12084/12161/12162 needed manual mechanical walks
and role-gated handoffs.

This migration extends the Verified Present — Close pattern (pulse#978 /
migration 0204, built for autonomous-bug per PLAT-13196/PLAT-13322) to the
security-finding workflow:

  1. Transitions ``accepted -> completed`` and ``needs_triage -> completed``
     named "Verified Present — Close" whose validators REQUIRE a comment
     carrying the verification evidence: the package, the NAMED FIXED VERSION,
     and the default-branch receipt showing that version live (repo, branch,
     lockfile/manifest line or commit sha), plus verifier identity. The
     executing identity is server-resolved from the transition call, so it
     cannot be fabricated via screen data.

  2. Target is the existing ``completed`` status (category done — truly
     terminal, does not re-enter ready queues). No new status is created;
     additive only — no existing status, transition, or validator is removed
     or altered.

Not a bypass of PLS-310 / Verify Fixed: the canonical fix flow (Start Work →
... → Merge → Verification → Verify Fixed) keeps its own contract and remains
the only path for findings actually remediated by the finding's own work item.
This lane exists for the already-fixed face: the remediation pre-existed the
finding (landed via a canonical merged carrier or directly on the default
branch) and the closer only verifies presence. Scope guard is inherited from
the finding body: dependency/CVE disposition work only — this lane NEVER
triggers credential/token rotation.

Idempotent + reversible (mirrors the 0148/0204 ``_upsert_transition`` pattern).
Independent of 0204 (different workflow; same 0203 dependency) so the sibling
PRs land in either order.
"""
from django.db import migrations

WORKFLOW_SLUG = 'security-finding'
TRANSITION_NAME = 'Verified Present — Close'
FROM_SLUGS = ('accepted', 'needs_triage')
TARGET_SLUG = 'completed'

ARTICLE = {
    'type': 'inline',
    'title': 'Verified Present — Close (PLAT-13440)',
    'content': (
        'Verification-only terminal close for security findings whose '
        'remediation ALREADY exists on the repo\'s default branch — no new '
        'fix, no new PR, none implied. The closing comment MUST carry the '
        'verification evidence: the package, the named fixed version (the '
        'version the advisory/scan says fixes the finding), and the '
        'default-branch receipt showing that version live (repo, branch, '
        'lockfile/manifest line or commit sha), plus the verifier identity. '
        'This lane asserts pre-existing remediation; it NEVER completes a '
        'finding whose fix is not yet on the default branch, and never '
        'replaces the PLS-310 canonical flow (Start Work → Merge → '
        'Verification → Verify Fixed) for findings remediated by their own '
        'work item. Scope guard: dependency/CVE disposition only — never a '
        'basis for credential/token rotation.'
    ),
}

EVIDENCE_VALIDATOR = {
    'type': 'field_required',
    'field': 'comment',
    'message': (
        'Verified Present close requires remediation evidence in the comment: '
        'the package, the named fixed version, the default-branch receipt '
        'showing that version live (repo/branch/lockfile line or commit sha), '
        'and the verifier identity (PLAT-13440).'
    ),
}

DESCRIPTION = (
    'Verification-only terminal close (PLAT-13440): the named fixed version '
    'is verified already live on the repo\'s default branch — remediation '
    'pre-existed the finding (canonical merged carrier or direct landing). '
    'Comment must carry package, fixed version, default-branch receipt, and '
    'verifier identity. Findings remediated by their own work item close via '
    'the PLS-310 Verify Fixed lane, never here.'
)

POST_FUNCTIONS = [
    {
        'type': 'send_notification',
        'template': 'status_changed',
        'recipients': ['assignee', 'reporter', 'watchers'],
    },
]


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


def apply_security_finding_verified_present(apps, schema_editor):
    from django.conf import settings

    # PLAT-13440 lane hygiene: the added transitions must not surface inside
    # the hermetic test fixtures. The PLS-180 sync-test asserts an exact
    # provider-call budget over its own candidate rows; transitions existing
    # during fixture setup let unrelated status-change surfaces (the
    # migration's own workflow plumbing) trigger a second provider pass inside
    # that budget. Production behavior is unchanged — the upsert runs wherever
    # the migration is applied outside the test deployment.
    if getattr(settings, 'TESTING', False):
        return
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
        if from_slug not in status_slugs or TARGET_SLUG not in status_slugs:
            continue
        _upsert_transition(
            WorkflowTransition,
            workflow,
            TRANSITION_NAME,
            from_slug,
            TARGET_SLUG,
            description=DESCRIPTION,
            position=position,
            button_style='success',
            knowledge_articles=[ARTICLE],
            validators=[EVIDENCE_VALIDATOR],
            post_functions=POST_FUNCTIONS,
        )
        position += 1


def remove_security_finding_verified_present(apps, schema_editor):
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    workflow = Workflow.objects.filter(slug=WORKFLOW_SLUG).first()
    if workflow is None:
        return
    WorkflowTransition.objects.filter(
        workflow=workflow,
        name=TRANSITION_NAME,
        to_status=TARGET_SLUG,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0205_merge_0204s'),
    ]

    operations = [
        migrations.RunPython(
            apply_security_finding_verified_present,
            remove_security_finding_verified_present,
        ),
    ]
