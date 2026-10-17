"""PLAT-4215 follow-up: a TEAM-OWNED blocker satisfies the blocked-task constraint.

The 0100 trigger required every blocked task's blocker to carry a CONCRETE
assignee (assignee_id + assignee_email) at every instant. That is stricter than
Pulse's own routing model: team assignment is authoritative (the /assign/ path
deliberately clears the concrete assignee when assignment_group is set, and the
availability resolver picks the live, least-loaded member afterwards). The two
rules were incompatible — re-routing a blocker task to a team while anything
was blocked on it raised
``blocked_item_requires_active_assigned_blocker`` at COMMIT, surfacing as
recurring 500s on /api/items/<key>/assign/ (observed on INV-32 whose dependent
INV-27 is blocked on it, 2026-07-14) and, worse, WEDGING the blocker on its
stale owner — the opposite of what PLAT-4215 wanted (progress on blockers).

New rule: the blocker must be an ACTIVE task that is OWNED, where owned means
  - a concrete assignee (assignee_id set AND assignee_email non-empty), OR
  - a non-empty assignment_group (team-owned; the team resolver guarantees a
    concrete member — CLAUDE.md assignment invariant).
"""

from django.db import migrations


RELAX_SQL = r"""
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


# Restore the 0100 (strict concrete-assignee) version on reverse.
STRICT_SQL = r"""
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
          AND blocker.assignee_id IS NOT NULL
          AND length(trim(coalesce(blocker.assignee_email, ''))) > 0
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
            DETAIL = 'PLAT-4215 requires every blocked task to have an active linked blocker with assignee_id and assignee_email.';
    END IF;
END;
$$;
"""


def relax_constraint(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(RELAX_SQL)


def restore_strict_constraint(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(STRICT_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0100_plat4215_blocked_owned_blocker'),
    ]

    operations = [
        migrations.RunPython(relax_constraint, restore_strict_constraint),
    ]
