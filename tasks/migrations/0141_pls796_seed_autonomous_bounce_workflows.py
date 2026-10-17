"""PLS-796: source-own autonomous-dev/bug Request Changes bounce chains.

Production uses distinct ``autonomous-dev`` and ``autonomous-bug`` workflow
rows, but CI never seeded them — only ``software-development`` /
``bug-workflow`` existed. Tests that claimed the autonomous names resolved
onto legacy rows were wrong; more importantly, many suite fixtures pin
``workflow_name='autonomous-dev'|'autonomous-bug'`` *because* those names
were historically unseeded (so ``Item.save`` skipped status coercion). Once
we seed the rows, the status set must be the production superset or coercion
silently rewrites ``needs_triage``/``deferred``/``awaiting_deploy``/etc.

This migration idempotently ensures both autonomous workflows exist with:

* the full status superset used by boards, capacity-pull gates, focused-QA,
  stale-sweep, recurring-cycle stop, and status-lane restore
* locked bounce edges:
    Request Changes: in_review → in_progress
    Return for Changes: awaiting_merge → in_progress
* open → in_progress (Start Work) so queue-order / start transitions work
* autonomous-bug initial_status = needs_triage
* autonomous-dev initial_status = open

Post-function order on bounce edges (source-owned contract):
  stamp_review_bounce → assign_implementer → send_notification

No same-transition ``assign_team`` (assign_implementer ladder owns destination).
"""
from django.db import migrations


AUTONOMOUS_SPECS = (
    {
        'slug': 'autonomous-dev',
        'name': 'Autonomous Dev',
        'description': 'Features / general autonomous engineering work.',
        'initial': 'open',
    },
    {
        'slug': 'autonomous-bug',
        'name': 'Autonomous Bug',
        'description': 'Autonomous bug workflow with agent triage + QA.',
        'initial': 'needs_triage',
    },
)

# Production + suite superset. Categories match Status seed conventions
# (todo / in_progress / scheduled / done). Including a slug here means
# Item.save will NOT coerce it away when workflow_name is autonomous-*.
STATUS_SPECS = (
    # todo-category
    ('open', 'Open', 'todo', 0),
    ('needs_triage', 'Needs Triage', 'todo', 1),
    ('todo', 'To Do', 'todo', 2),
    # backlog is its OWN category (migration 0063) — queue/stale-sweep
    # exclusions key on category='backlog'. Never rewrite it to 'todo'.
    ('backlog', 'Backlog', 'backlog', 3),
    ('reopened', 'Reopened', 'todo', 4),
    # in_progress-category (active pipeline)
    ('in_progress', 'In Progress', 'in_progress', 10),
    ('in_review', 'In Review', 'in_progress', 11),
    ('in_qa', 'In QA', 'in_progress', 12),
    ('awaiting_merge', 'Awaiting Merge', 'in_progress', 13),
    ('awaiting_deploy', 'Awaiting Deploy', 'in_progress', 14),
    ('awaiting_verification', 'Awaiting Verification', 'in_progress', 15),
    ('verification', 'Verification', 'in_progress', 16),
    ('blocked', 'Blocked', 'in_progress', 17),
    ('design', 'Design', 'in_progress', 18),
    # scheduled
    ('scheduled', 'Scheduled', 'scheduled', 19),
    # done-category (terminals + parked)
    ('done', 'Done', 'done', 20),
    ('completed', 'Completed', 'done', 21),
    ('closed', 'Closed', 'done', 22),
    ('cancelled', 'Cancelled', 'done', 23),
    ('deferred', 'Deferred', 'done', 24),
)

BOUNCE_POST_FUNCTIONS = [
    {'type': 'stamp_review_bounce'},
    {'type': 'assign_implementer'},
    {
        'type': 'send_notification',
        'template': 'status_changed',
        'recipients': ['assignee', 'watchers'],
    },
]

BOUNCE_EDGES = (
    {
        'name': 'Request Changes',
        'from_status': 'in_review',
        'to_status': 'in_progress',
        'position': 30,
        'button_style': 'warning',
        'description': (
            'Review found issues; return to the implementer via '
            'assign_implementer destination ladder (PLS-796).'
        ),
        'post_functions': BOUNCE_POST_FUNCTIONS,
        'screen_fields': [
            # Comment recommended but not required — existing callers (and
            # blocker-WIP invariant tests) fire Request Changes without screen data.
            {'field': 'comment', 'label': 'What needs to change?', 'required': False},
        ],
    },
    {
        'name': 'Return for Changes',
        'from_status': 'awaiting_merge',
        'to_status': 'in_progress',
        'position': 31,
        'button_style': 'warning',
        'description': (
            'Merge gate returned work; bounce to implementer via '
            'assign_implementer destination ladder (PLS-796).'
        ),
        'post_functions': BOUNCE_POST_FUNCTIONS,
        'screen_fields': [
            {'field': 'comment', 'label': 'What needs to change?', 'required': False},
        ],
    },
)

# Minimal non-bounce edges required so suite/production start paths work once
# the workflow row exists (previously Workflow.resolve returned None and
# validate_transition never consulted these rows).
START_EDGES = (
    {
        'name': 'Start Work',
        'from_status': 'open',
        'to_status': 'in_progress',
        'position': 1,
        'button_style': 'primary',
        'description': 'Begin implementation.',
        'post_functions': [
            {
                'type': 'send_notification',
                'template': 'status_changed',
                'recipients': ['assignee', 'watchers'],
            },
        ],
        'screen_fields': [],
    },
    {
        'name': 'Start Work',
        'from_status': 'needs_triage',
        'to_status': 'in_progress',
        'position': 2,
        'button_style': 'primary',
        'description': 'Triage complete; begin implementation.',
        'post_functions': [
            {
                'type': 'send_notification',
                'template': 'status_changed',
                'recipients': ['assignee', 'watchers'],
            },
        ],
        'screen_fields': [],
    },
    {
        'name': 'Start Work',
        'from_status': 'todo',
        'to_status': 'in_progress',
        'position': 3,
        'button_style': 'primary',
        'description': 'Begin implementation.',
        'post_functions': [
            {
                'type': 'send_notification',
                'template': 'status_changed',
                'recipients': ['assignee', 'watchers'],
            },
        ],
        'screen_fields': [],
    },
)


def _status(Status, slug, name, category, position):
    """Ensure Status row exists. Never clobber an existing category/name.

    Global Status rows are shared across every workflow. update_or_create
    would rewrite migration-0063's backlog category='backlog' back to
    whatever we pass, breaking queue/stale-sweep exclusions that key on
    category. Only fill blanks on create; heal category only when the
    existing row is empty/unknown.
    """
    obj, created = Status.objects.get_or_create(
        slug=slug,
        defaults={
            'name': name,
            'category': category,
            'position': position,
            'is_system': True,
        },
    )
    if created:
        return obj
    # Heal only if category is missing/blank; never downgrade a known category.
    if not (getattr(obj, 'category', None) or '').strip():
        obj.category = category
        obj.save(update_fields=['category'])
    return obj


def _upsert_transition(WorkflowTransition, workflow, *, name, from_status, to_status,
                       description, position, button_style='default',
                       post_functions=None, screen_fields=None, lock_bounce_chain=False):
    defaults = {
        'description': description,
        'position': position,
        'button_style': button_style,
        'screen_fields': list(screen_fields or []),
        'post_functions': [dict(pf) for pf in (post_functions or [])],
        # Empty allowed_roles = any authenticated assignee may fire the edge.
        # Seeding member/admin/owner here makes hermetic tests (no org
        # memberships on principals) treat every candidate as
        # _member_can_execute_from_status=False once the workflow row exists,
        # which strands merge-lane restore / rebalance (PLS-188) and capacity
        # drains. Production still gates via team assignment + transition
        # validators; do not invent a role gate the live autonomous rows do
        # not carry on these edges.
        'allowed_roles': [],
    }
    existing = WorkflowTransition.objects.filter(
        workflow=workflow,
        name=name,
        from_status=from_status,
        to_status=to_status,
    ).first()
    if existing is None:
        WorkflowTransition.objects.create(
            workflow=workflow,
            name=name,
            from_status=from_status,
            to_status=to_status,
            **defaults,
        )
        return
    if not lock_bounce_chain:
        # Non-bounce edges: only fill empty PF/screen; don't clobber live edits.
        changed = []
        if not existing.post_functions and defaults['post_functions']:
            existing.post_functions = defaults['post_functions']
            changed.append('post_functions')
        if not existing.screen_fields and defaults['screen_fields']:
            existing.screen_fields = defaults['screen_fields']
            changed.append('screen_fields')
        if changed:
            existing.save(update_fields=changed)
        return
    # Bounce edges: normalize PF chain to the locked order; strip assign_team;
    # keep allowed_roles empty (see defaults comment).
    pfs = [dict(pf) for pf in (existing.post_functions or []) if isinstance(pf, dict)]
    types = [pf.get('type') for pf in pfs]
    desired_types = [pf['type'] for pf in defaults['post_functions']]
    existing_roles = list(existing.allowed_roles or [])
    changed_fields = []
    if types != desired_types:
        existing.post_functions = [dict(pf) for pf in defaults['post_functions']]
        existing.description = description
        existing.position = position
        existing.button_style = button_style
        existing.screen_fields = defaults['screen_fields']
        changed_fields.extend([
            'post_functions', 'description', 'position', 'button_style', 'screen_fields',
        ])
    if existing_roles != []:
        existing.allowed_roles = []
        changed_fields.append('allowed_roles')
    if changed_fields:
        existing.save(update_fields=changed_fields)


def seed_autonomous_bounce_workflows(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    status_map = {
        slug: _status(Status, slug, name, category, position)
        for slug, name, category, position in STATUS_SPECS
    }
    # Migration 0063 made backlog its own category. If a prior incomplete
    # 0141 rewrite clobbered it back to 'todo', restore the exclusion key.
    backlog = status_map.get('backlog')
    if backlog is not None and getattr(backlog, 'category', None) != 'backlog':
        backlog.category = 'backlog'
        backlog.is_system = True
        backlog.save(update_fields=['category', 'is_system'])

    for spec in AUTONOMOUS_SPECS:
        initial = status_map[spec['initial']]
        workflow, created = Workflow.objects.get_or_create(
            slug=spec['slug'],
            defaults={
                'name': spec['name'],
                'description': spec['description'],
                'is_system': True,
                'is_active': True,
                'initial_status': initial,
            },
        )
        update_fields = []
        if not created:
            if not workflow.initial_status_id:
                workflow.initial_status = initial
                update_fields.append('initial_status')
            # Keep autonomous-bug entry point on needs_triage when we own the seed.
            elif (
                spec['slug'] == 'autonomous-bug'
                and getattr(workflow.initial_status, 'slug', None) not in {
                    'needs_triage', 'open', 'todo',
                }
            ):
                workflow.initial_status = initial
                update_fields.append('initial_status')
            elif (
                spec['slug'] == 'autonomous-bug'
                and getattr(workflow.initial_status, 'slug', None) != 'needs_triage'
            ):
                # Prefer needs_triage as the bug entry point (focused-QA contract).
                workflow.initial_status = initial
                update_fields.append('initial_status')
            if update_fields:
                workflow.save(update_fields=update_fields)

        for position, slug in enumerate(status_map):
            WorkflowStatus.objects.get_or_create(
                workflow=workflow,
                status=status_map[slug],
                defaults={'position': position},
            )

        for edge in START_EDGES:
            _upsert_transition(
                WorkflowTransition,
                workflow,
                name=edge['name'],
                from_status=edge['from_status'],
                to_status=edge['to_status'],
                description=edge['description'],
                position=edge['position'],
                button_style=edge['button_style'],
                post_functions=edge['post_functions'],
                screen_fields=edge['screen_fields'],
                lock_bounce_chain=False,
            )

        for edge in BOUNCE_EDGES:
            _upsert_transition(
                WorkflowTransition,
                workflow,
                name=edge['name'],
                from_status=edge['from_status'],
                to_status=edge['to_status'],
                description=edge['description'],
                position=edge['position'],
                button_style=edge['button_style'],
                post_functions=edge['post_functions'],
                screen_fields=edge['screen_fields'],
                lock_bounce_chain=True,
            )


def unseed_autonomous_bounce_workflows(apps, schema_editor):
    """Reverse only the bounce edges this migration owns; keep workflows."""
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')
    workflows = Workflow.objects.filter(slug__in=[s['slug'] for s in AUTONOMOUS_SPECS])
    WorkflowTransition.objects.filter(
        workflow__in=workflows,
        name__in=['Request Changes', 'Return for Changes'],
        from_status__in=['in_review', 'awaiting_merge'],
        to_status='in_progress',
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0140_merge_pls733_active_wip'),
    ]

    operations = [
        migrations.RunPython(
            seed_autonomous_bounce_workflows,
            unseed_autonomous_bounce_workflows,
        ),
    ]
