"""PLS-719: block-releasing `resolved_elsewhere` status for escalation blocker children.

PLAT-4215 and the resume article form an unsatisfiable cycle when a blocker
child's ask is resolved on the parent: the child cannot terminate while the
parent is blocked (PLAT-4215 requires an owned active blocker), and the parent
should not resume until the child terminates (the resume article's precondition).
For escalation blocker children (Raise to Admin Ops / Escalate to Leads) the
child is a token for an ask whose answer is recorded on the PARENT — nothing
ever licenses the child to terminate on its own evidence.

Fix: introduce a NON-terminal, block-releasing status `resolved_elsewhere` that
a blocker child can occupy meaning "the ask was resolved elsewhere". It is in
`BLOCK_RELEASING_NONTERMINAL_SLUGS` (workflow_service.py) so it releases the
parent, and it is added to the deferred Postgres trigger's NOT IN list so the
ORM predicate and the trigger stay aligned. The `Resume After Blocker
Resolution` article is updated to state the exception.

This status is NOT `waiting_external` (PLS-707 Constraint 4): that status is
excluded from assignment/capacity/load and is explicitly NOT block-releasing —
it means "nobody here can act on this", the opposite claim on the same axis.
"""

from django.db import migrations

# The 0101 (team-owned) trigger with `resolved_elsewhere` added to the NOT IN
# list — the ORM predicate (`_status_releases_blocks`) and the deferred trigger
# must agree on which blocker statuses release their dependents.
TRIGGER_SQL = r"""
CREATE OR REPLACE FUNCTION tasks_assert_blocked_has_owned_blocker(p_item_id bigint)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM tasks_task parent
        WHERE parent.id = p_item_id AND lower(parent.status) = 'blocked'
    ) AND NOT EXISTS (
        SELECT 1
        FROM tasks_item_link link
        JOIN tasks_task blocker
          ON blocker.id = CASE
              WHEN link.link_type = 'blocks' AND link.target_item_id = p_item_id
                  THEN link.source_item_id
              WHEN link.link_type IN ('is_blocked_by', 'blocked_by')
                   AND link.source_item_id = p_item_id
                  THEN link.target_item_id
              ELSE NULL
          END
        WHERE (
            (link.link_type = 'blocks' AND link.target_item_id = p_item_id)
            OR
            (link.link_type IN ('is_blocked_by', 'blocked_by') AND link.source_item_id = p_item_id)
        )
          AND (
              -- concretely assigned ...
              (blocker.assignee_id IS NOT NULL
               AND length(trim(coalesce(blocker.assignee_email, ''))) > 0)
              -- ... OR team-owned (routing resolves the concrete member async)
              OR length(trim(coalesce(blocker.assignment_group, ''))) > 0
          )
          AND lower(trim(coalesce(blocker.mode, ''))) = 'task'
          AND lower(replace(blocker.status, '-', '_')) NOT IN (
              'completed', 'done', 'closed', 'cancelled', 'canceled', 'resolved',
              'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired',
              'deferred', 'silenced', 'applied', 'change_rejected',
              'rfc_approved', 'rfc_implementation_linked', 'rfc_validation',
              'resolved_elsewhere'
          )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = format(
                'blocked_item_requires_active_assigned_blocker: item_id=%s',
                p_item_id
            ),
            DETAIL = 'PLAT-4215 requires every blocked task to have an active linked blocker that is owned: concretely assigned (assignee_id + assignee_email) or routed to a team (assignment_group).';
    END IF;
END;
$$;
"""

# The pre-PLS-719 trigger (0101 team-owned, without resolved_elsewhere).
PREVIOUS_TRIGGER_SQL = r"""
CREATE OR REPLACE FUNCTION tasks_assert_blocked_has_owned_blocker(p_item_id bigint)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM tasks_task parent
        WHERE parent.id = p_item_id AND lower(parent.status) = 'blocked'
    ) AND NOT EXISTS (
        SELECT 1
        FROM tasks_item_link link
        JOIN tasks_task blocker
          ON blocker.id = CASE
              WHEN link.link_type = 'blocks' AND link.target_item_id = p_item_id
                  THEN link.source_item_id
              WHEN link.link_type IN ('is_blocked_by', 'blocked_by')
                   AND link.source_item_id = p_item_id
                  THEN link.target_item_id
              ELSE NULL
          END
        WHERE (
            (link.link_type = 'blocks' AND link.target_item_id = p_item_id)
            OR
            (link.link_type IN ('is_blocked_by', 'blocked_by') AND link.source_item_id = p_item_id)
        )
          AND (
              (blocker.assignee_id IS NOT NULL
               AND length(trim(coalesce(blocker.assignee_email, ''))) > 0)
              OR length(trim(coalesce(blocker.assignment_group, ''))) > 0
          )
          AND lower(trim(coalesce(blocker.mode, ''))) = 'task'
          AND lower(replace(blocker.status, '-', '_')) NOT IN (
              'completed', 'done', 'closed', 'cancelled', 'canceled', 'resolved',
              'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired',
              'deferred', 'silenced', 'applied', 'change_rejected',
              'rfc_approved', 'rfc_implementation_linked', 'rfc_validation'
          )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = format(
                'blocked_item_requires_active_assigned_blocker: item_id=%s',
                p_item_id
            ),
            DETAIL = 'PLAT-4215 requires every blocked task to have an active linked blocker that is owned: concretely assigned (assignee_id + assignee_email) or routed to a team (assignment_group).';
    END IF;
END;
$$;
"""

# Updated resume article: states the block-releasing exception (PLS-719) so the
# article and the 409 no longer disagree.
RESUME_ARTICLE_CONTENT = (
    'Use only when linked blockers have actually reached terminal states — or '
    'a block-releasing non-terminal status (e.g. `resolved_elsewhere`, where a '
    'blocker child\'s ask was answered on the parent). The automatic unblock '
    'path normally restores the pre-block status and owner; manual use is a '
    'recovery path. PLS-719: a blocker child whose ask is resolved on the '
    'parent may be moved to `resolved_elsewhere` (block-releasing) so the '
    'parent can resume without the child needing to terminate on its own '
    'evidence.'
)


def _upsert_transition(WorkflowTransition, workflow, name, from_status, to_status, **defaults):
    transition = WorkflowTransition.objects.filter(
        workflow=workflow,
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
    transition.save()


def apply(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    # 1. Create the global `resolved_elsewhere` status (non-terminal, category
    #    'todo' so it is not conflated with done/closed terminal states).
    status, _ = Status.objects.update_or_create(
        slug='resolved_elsewhere',
        defaults={
            'name': 'Resolved Elsewhere',
            'category': 'todo',
            'color': '#8b5cf6',
            'position': 99,
            'is_system': True,
            'description': (
                'The blocker child\'s ask was answered on the parent (a routing/'
                'ownership decision recorded on the dependent, not the child). '
                'Block-releasing: the parent may resume while the child records '
                '"ask resolved elsewhere". NOT waiting_external — this status '
                'releases blocks; waiting_external explicitly does not.'
            ),
        },
    )

    # 2. Add the status to the `simple` workflow (the workflow escalation blocker
    #    children are created in) and wire transitions to reach it.
    simple_wf = Workflow.objects.filter(slug='simple').first()
    if simple_wf is not None:
        if not WorkflowStatus.objects.filter(workflow=simple_wf, status=status).exists():
            WorkflowStatus.objects.create(
                workflow=simple_wf,
                status=status,
                position=99,
            )
        for source in ('todo', 'in_progress'):
            _upsert_transition(
                WorkflowTransition,
                simple_wf,
                'Resolved Elsewhere',
                source,
                'resolved_elsewhere',
                description=(
                    'The blocker child\'s ask was answered on the parent. '
                    'Block-releasing: the parent may resume while the child '
                    'records "ask resolved elsewhere" (PLS-719).'
                ),
                position=90,
            )

    # 3. Recreate the deferred trigger with `resolved_elsewhere` in the NOT IN
    #    list so the ORM predicate and the trigger stay aligned.
    if schema_editor.connection.vendor == 'postgresql':
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(TRIGGER_SQL)

    # 4. Update the `Resume After Blocker Resolution` knowledge article so it
    #    states the block-releasing exception (article and 409 no longer
    #    disagree).
    WorkflowTransition.objects.filter(
        name__startswith='Blocker Resolved - Resume',
    ).update(
        knowledge_articles=[{
            'type': 'inline',
            'title': 'Resume After Blocker Resolution',
            'content': RESUME_ARTICLE_CONTENT,
        }],
    )


def unapply(apps, schema_editor):
    Status = apps.get_model('tasks', 'Status')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')
    WorkflowTransition = apps.get_model('tasks', 'WorkflowTransition')

    # Remove the transitions + workflow status from the simple workflow.
    simple_wf = Workflow.objects.filter(slug='simple').first()
    if simple_wf is not None:
        WorkflowTransition.objects.filter(
            workflow=simple_wf,
            name='Resolved Elsewhere',
        ).delete()
        WorkflowStatus.objects.filter(
            workflow=simple_wf,
            status__slug='resolved_elsewhere',
        ).delete()

    # Restore the pre-PLS-719 trigger.
    if schema_editor.connection.vendor == 'postgresql':
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(PREVIOUS_TRIGGER_SQL)

    # Restore the pre-PLS-719 resume article.
    WorkflowTransition.objects.filter(
        name__startswith='Blocker Resolved - Resume',
    ).update(
        knowledge_articles=[{
            'type': 'inline',
            'title': 'Resume After Blocker Resolution',
            'content': (
                'Use only when linked blockers have actually reached terminal '
                'states. The automatic unblock path normally restores the '
                'pre-block status and owner; manual use is a recovery path.'
            ),
        }],
    )

    # Leave the global Status row in place (removing it could orphan workflow
    # references); it is harmless and reusable.


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0171_pls962_external_response_hold'),
    ]

    operations = [
        migrations.RunPython(apply, unapply),
    ]
