"""PLS-825 (PLS-819 S2): seed the dedicated focused-qa workflow.

Introduces the system workflow slug ``focused-qa`` with typed PASS / FAIL /
CORRECT / SUBSUMED disposition transitions, replacing the overloaded generic
``Request Changes`` edge on ``reviewed-task`` (HLD §2, §3). Reuses existing
global status rows; adds no new status slug. Idempotent upsert; reverse is a
no-op (HLD §6 — no destructive reverse migration is allowed).

Transitions seeded (HLD §3 state machine):
  todo -> in_progress      Start Verification
  in_progress -> in_review Submit Evidence Review
  in_review -> done        Pass QA Evidence (PASS)
  in_review -> done        Confirm Product Failure (FAIL)
  in_review -> in_progress Return Evidence for Correction (CORRECT)
  in_review -> done        Close Duplicate / Subsumed (SUBSUMED)
  active -> blocked        Blocked by Linked Issue
  blocked -> active        Blocker Resolved - Resume <status>
  pre-review -> cancelled  Cancel Invalid/Test Artifact
"""
from django.db import migrations

WORKFLOW_SLUG = 'focused-qa'

# ---------------------------------------------------------------------------
# Knowledge articles (reviewer guidance — HLD §2.2, §5)
# ---------------------------------------------------------------------------

KB_WORKFLOW = [
    {
        'type': 'inline',
        'title': 'Focused-QA Evidence Review (not code review)',
        'content': (
            'This workflow reviews live-QA evidence, not code. A focused-QA task '
            'is independently reviewable WITHOUT a linked Origin PR. Assess the '
            'QA Evidence Standard: observation time/environment, exact build when '
            'relevant, tester identity, exercised path, expected/observed result, '
            'and UI/log evidence where applicable. Code PR targeting rules apply '
            'to code workflows, not this workflow.'
        ),
    },
]

KB_START = [
    {
        'type': 'inline',
        'title': 'Start Verification',
        'content': (
            'Begin live verification of the target surface. Retain the exact-org '
            'QA verifier route and preserve all recorded SoD exclusions. Do not '
            'borrow a verifier from another organization.'
        ),
    },
]

KB_SUBMIT = [
    {
        'type': 'inline',
        'title': 'Submit Evidence Review',
        'content': (
            'Submit the completed evidence for independent review. The verifier '
            'is stashed and an independent Review seat is assigned. Evidence must '
            'meet the QA Evidence Standard before submission.'
        ),
    },
]

KB_PASS = [
    {
        'type': 'inline',
        'title': 'Pass QA Evidence',
        'content': (
            'Use only when the evidence satisfies the QA Evidence Standard: '
            'observation time/environment, exact build when relevant, tester '
            'identity, exercised path, expected/observed result, and UI/log '
            'evidence. Commits an immutable PASS receipt; creates no product bug.'
        ),
    },
]

KB_FAIL = [
    {
        'type': 'inline',
        'title': 'Confirm Product Failure',
        'content': (
            'Use when the deployed product failed live QA. Requires bounded '
            'failure evidence: summary, exact reproduction steps, observed build '
            'identity, expected vs observed, and a canonical failure fingerprint. '
            'Commits an immutable FAIL receipt and creates exactly one owning-'
            'project bug reference.'
        ),
    },
]

KB_CORRECT = [
    {
        'type': 'inline',
        'title': 'Return Evidence for Correction',
        'content': (
            'Use when the evidence is incomplete or incorrect but no product '
            'failure is asserted. Restores the stashed QA verifier and exact-org '
            'QA route. Commits a CORRECT receipt; creates no product bug and does '
            'not change the originating development item.'
        ),
    },
]

KB_SUBSUMED = [
    {
        'type': 'inline',
        'title': 'Close Duplicate / Subsumed',
        'content': (
            'Use when this verification is a duplicate of, or subsumed by, another '
            'task. Requires a same-org target task key and a reason. Writes the '
            'duplicate/relation link and commits a SUBSUMED receipt. Preserves '
            'protected review history; do NOT use Cancel or Approve for this.'
        ),
    },
]

KB_CANCEL = [
    {
        'type': 'inline',
        'title': 'Cancel Invalid/Test Artifact',
        'content': (
            'Use only for invalid or test-only artifacts BEFORE any review history '
            'exists. Once evidence has been submitted for review, protected review '
            'history must never be erased to close the task — use Close Duplicate / '
            'Subsumed instead. Always include a short comment.'
        ),
    },
]

KB_BLOCK = [
    {
        'type': 'inline',
        'title': 'Blocked by Linked Issue',
        'content': (
            'Use when this task has an active linked blocker and no meaningful '
            'verification can continue until that blocker resolves.'
        ),
    },
]

KB_RESUME = [
    {
        'type': 'inline',
        'title': 'Resume After Blocker Resolution',
        'content': (
            'Use only when linked blockers have actually reached terminal states. '
            'The automatic unblock path normally restores the pre-block status and '
            'owner; manual use is a recovery path.'
        ),
    },
]

# ---------------------------------------------------------------------------
# Post-functions
# ---------------------------------------------------------------------------

PF_NOTIFY = [
    {'type': 'send_notification', 'template': 'status_changed', 'recipients': ['assignee', 'watchers']},
]
PF_NOTIFY_ALL = [
    {'type': 'send_notification', 'template': 'status_changed', 'recipients': ['assignee', 'reporter', 'watchers']},
]


def _status(Status, slug, **defaults):
    status, _ = Status.objects.update_or_create(slug=slug, defaults=defaults)
    return status


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
    changed = []
    for field, value in defaults.items():
        setattr(transition, field, value)
        changed.append(field)
    if changed:
        transition.save(update_fields=changed)


def seed_focused_qa_workflow(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Resolution = apps.get_model('tasks', 'Resolution')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    statuses = {
        'todo': _status(
            Status, 'todo', name='To Do', category='todo', color='#6b7280',
            position=1, is_system=True, description='Item is ready to be worked on',
        ),
        'in_progress': _status(
            Status, 'in_progress', name='In Progress', category='in_progress',
            color='#3b82f6', position=10, is_system=True,
            description='Work is actively being done',
        ),
        'in_review': _status(
            Status, 'in_review', name='In Review', category='in_progress',
            color='#8b5cf6', position=11, is_system=False,
            description='Evidence is complete and awaiting independent review',
        ),
        'blocked': _status(
            Status, 'blocked', name='Blocked', category='in_progress',
            color='#f59e0b', position=30, is_system=True,
            description='Work cannot progress until one or more linked blockers are resolved.',
        ),
        'done': _status(
            Status, 'done', name='Done', category='done', color='#22c55e',
            position=20, is_system=True, description='Work is complete',
        ),
        'cancelled': _status(
            Status, 'cancelled', name='Cancelled', category='done', color='#9ca3af',
            position=22, is_system=False,
            description='Item was cancelled without completion',
        ),
    }

    for slug, name in (('passed', 'Passed'), ('failed', 'Failed'), ('duplicate', 'Duplicate'),
                       ('superseded', 'Superseded'), ('correction_requested', 'Correction Requested')):
        Resolution.objects.update_or_create(
            slug=slug,
            defaults={'name': name, 'position': 50, 'is_system': True},
        )
    Resolution.objects.update_or_create(
        slug='cancelled',
        defaults={'name': 'Cancelled', 'position': 9, 'is_system': False,
                  'description': 'Work was cancelled or discarded without completion'},
    )

    workflow, _ = Workflow.objects.update_or_create(
        slug=WORKFLOW_SLUG,
        defaults={
            'name': 'Focused QA',
            'description': (
                'Dedicated live-QA evidence review workflow with typed PASS / FAIL / '
                'CORRECT / SUBSUMED dispositions. Independent review remains mandatory; '
                'a focused-QA task is reviewable by workflow identity and does not '
                'require a linked Origin PR.'
            ),
            'is_system': True,
            'is_active': True,
            'initial_status': statuses['todo'],
            'knowledge_base': KB_WORKFLOW,
        },
    )

    for position, slug in enumerate(['todo', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled']):
        WorkflowStatus.objects.update_or_create(
            workflow=workflow,
            status=statuses[slug],
            defaults={'position': position},
        )

    _upsert_transition(
        WorkflowTransition, workflow,
        'Start Verification', 'todo', 'in_progress',
        description='Begin live verification of the target surface.',
        position=0, button_style='primary',
        knowledge_articles=KB_START,
        post_functions=PF_NOTIFY,
    )
    _upsert_transition(
        WorkflowTransition, workflow,
        'Submit Evidence Review', 'in_progress', 'in_review',
        description='Evidence complete; send to an independent Review seat.',
        position=10, button_style='primary',
        knowledge_articles=KB_SUBMIT,
        post_functions=[
            {'type': 'stash_implementer'},
            {'type': 'assign_team', 'value': 'review'},
            {'type': 'stamp_review_started'},
            {'type': 'send_notification', 'template': 'status_changed',
             'recipients': ['assignee', 'watchers']},
        ],
    )
    _upsert_transition(
        WorkflowTransition, workflow,
        'Pass QA Evidence', 'in_review', 'done',
        description='Evidence satisfies the QA Evidence Standard; complete the task.',
        position=20, button_style='success',
        screen_fields=[
            {'field': 'comment', 'label': 'Evidence assessment (required)', 'required': True},
        ],
        validators=[
            {'type': 'field_required', 'field': 'comment',
             'message': 'PASS requires an evidence assessment comment.'},
        ],
        knowledge_articles=KB_PASS,
        post_functions=[
            {'type': 'set_resolution', 'resolution': 'passed'},
            {'type': 'send_notification', 'template': 'status_changed',
             'recipients': ['assignee', 'reporter', 'watchers']},
        ],
    )
    _upsert_transition(
        WorkflowTransition, workflow,
        'Confirm Product Failure', 'in_review', 'done',
        description='Deployed product failed live QA; create the owning-project bug.',
        position=21, button_style='danger',
        screen_fields=[
            {'field': 'qa_failure_evidence', 'label': 'Bounded failure evidence (required)', 'required': True},
        ],
        validators=[
            {'type': 'field_required', 'field': 'qa_failure_evidence',
             'message': 'FAIL requires bounded failure evidence.'},
        ],
        knowledge_articles=KB_FAIL,
        post_functions=[
            {'type': 'set_resolution', 'resolution': 'failed'},
            {'type': 'send_notification', 'template': 'status_changed',
             'recipients': ['assignee', 'reporter', 'watchers']},
        ],
    )
    _upsert_transition(
        WorkflowTransition, workflow,
        'Return Evidence for Correction', 'in_review', 'in_progress',
        description='Evidence incomplete/incorrect; return to the QA verifier.',
        position=30, button_style='warning',
        screen_fields=[
            {'field': 'comment', 'label': 'What needs to change?', 'required': True},
        ],
        validators=[
            {'type': 'field_required', 'field': 'comment',
             'message': 'CORRECT requires a correction instruction.'},
        ],
        knowledge_articles=KB_CORRECT,
        post_functions=[
            {'type': 'assign_implementer'},
            {'type': 'send_notification', 'template': 'status_changed',
             'recipients': ['assignee', 'watchers']},
        ],
    )
    _upsert_transition(
        WorkflowTransition, workflow,
        'Close Duplicate / Subsumed', 'in_review', 'done',
        description='Duplicate or subsumed by another task; close without asserting failure.',
        position=31, button_style='default',
        screen_fields=[
            {'field': 'target_task', 'label': 'Same-org target task key (required)', 'required': True},
            {'field': 'comment', 'label': 'Reason (required)', 'required': True},
        ],
        validators=[
            {'type': 'field_required', 'field': 'target_task',
             'message': 'SUBSUMED requires a same-org target task key.'},
            {'type': 'field_required', 'field': 'comment',
             'message': 'SUBSUMED requires a reason.'},
        ],
        knowledge_articles=KB_SUBSUMED,
        post_functions=[
            {'type': 'set_resolution', 'resolution': 'duplicate'},
            {'type': 'send_notification', 'template': 'status_changed',
             'recipients': ['assignee', 'reporter', 'watchers']},
        ],
    )

    active_sources = ['todo', 'in_progress', 'in_review']
    for source in active_sources:
        _upsert_transition(
            WorkflowTransition, workflow,
            'Blocked by Linked Issue', source, 'blocked',
            description=(
                'Use when this task has an active linked blocker and no meaningful '
                'verification can continue until that blocker resolves.'
            ),
            position=40, button_style='warning',
            knowledge_articles=KB_BLOCK,
            post_functions=PF_NOTIFY,
        )
    for target in active_sources:
        _upsert_transition(
            WorkflowTransition, workflow,
            f'Blocker Resolved - Resume {target.replace("_", " ").title()}',
            'blocked', target,
            description=(
                'Recovery path after all linked blockers are terminal. Pulse '
                'usually executes this automatically and restores the pre-block owner.'
            ),
            position=51, button_style='primary',
            knowledge_articles=KB_RESUME,
            post_functions=PF_NOTIFY,
        )

    # Cancel is pre-review only (HLD §3): forbidden after protected review history.
    for source in ['todo', 'in_progress']:
        _upsert_transition(
            WorkflowTransition, workflow,
            'Cancel Invalid/Test Artifact', source, 'cancelled',
            description='Terminally cancel invalid or test-only work before review history exists.',
            position=900, button_style='danger',
            screen_fields=[
                {'field': 'comment', 'label': 'Why is this being cancelled?', 'required': True},
            ],
            knowledge_articles=KB_CANCEL,
            post_functions=[
                {'type': 'set_resolution', 'resolution': 'cancelled'},
                {'type': 'send_notification', 'template': 'status_changed',
                 'recipients': ['assignee', 'reporter', 'watchers']},
            ],
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0165_pls756_cancel_reason_category_screen_field'),
    ]

    operations = [
        migrations.RunPython(seed_focused_qa_workflow, noop),
    ]
