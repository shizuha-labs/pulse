"""Enforce blocked => active, concretely-assigned blocker task (PLAT-4215)."""

import json
import re

from django.db import migrations


SOURCE = 'PLAT-4215'


def _post_functions(value):
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, list):
            return [entry for entry in parsed if isinstance(entry, dict)]
    return []


def _dict_list(value):
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, dict)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, list):
            return [entry for entry in parsed if isinstance(entry, dict)]
    return []


AUTOMATIC_BLOCKER_GUIDANCE = (
    "Pulse transactionally creates, assigns, and links the blocker child before "
    "the parent enters `blocked`. Do not call `pulse_create_blocker_task` after "
    "a successful escalation; that would create a duplicate. If the transition "
    "fails, report the failure and leave the parent in its previous status."
)


def _manual_blocker_guidance(team):
    return (
        'After firing this transition: call '
        'mcp__shizuha-pulse__pulse_create_blocker_task('
        f'parent_id=this_task_id, team_slug="{team}", title=..., '
        'description=...) so the owner has a durable task on their queue.'
    )


def _restore_manual_blocker_guidance(text, team):
    """Reverse automatic-child guidance when the post-function is removed."""
    text = str(text or '')
    manual = _manual_blocker_guidance(team)
    text = text.replace(
        f'After firing this transition: {AUTOMATIC_BLOCKER_GUIDANCE}',
        manual,
    )
    return text.replace(AUTOMATIC_BLOCKER_GUIDANCE, manual)


def _rewrite_manual_blocker_guidance(text):
    """Replace the obsolete post-transition manual-child instruction."""
    text = str(text or '')
    text = re.sub(
        r'After firing this transition:\s*call\s+[^\n]*pulse_create_blocker_task[^\n]*',
        f'After firing this transition: {AUTOMATIC_BLOCKER_GUIDANCE}',
        text,
        flags=re.IGNORECASE,
    )
    return text


def _seed_automatic_blocker_guidance(transition):
    changed = []
    description = _rewrite_manual_blocker_guidance(transition.description)
    if 'transactionally creates, assigns, and links the blocker child' not in description:
        description = (description.rstrip() + '\n\n' + AUTOMATIC_BLOCKER_GUIDANCE).strip()
    if description != transition.description:
        transition.description = description
        changed.append('description')

    articles = _dict_list(transition.knowledge_articles)
    rewritten = []
    for article in articles:
        article = dict(article)
        if 'content' in article:
            article['content'] = _rewrite_manual_blocker_guidance(article.get('content'))
        rewritten.append(article)
    if not any(
        AUTOMATIC_BLOCKER_GUIDANCE in str(article.get('content') or '')
        for article in rewritten
    ):
        rewritten.append({
            'type': 'inline',
            'title': 'Automatic assigned blocker child (PLAT-4215)',
            'content': AUTOMATIC_BLOCKER_GUIDANCE,
        })
    if rewritten != articles:
        transition.knowledge_articles = rewritten
        changed.append('knowledge_articles')
    return changed


def seed_assigned_blocker_post_functions(apps, schema_editor):
    Transition = apps.get_model('tasks', 'WorkflowTransition')

    for transition in Transition.objects.filter(to_status='blocked'):
        name = (transition.name or '').strip().lower()
        pfs = _post_functions(transition.post_functions)
        has_blocker_pf = any(pf.get('type') == 'create_blocker_task' for pf in pfs)

        team = ''
        for pf in pfs:
            if pf.get('type') == 'assign_team':
                team = str(pf.get('value') or pf.get('team') or '').strip().lower()
                break
            if pf.get('type') == 'assign_to' and pf.get('target') == 'operator':
                team = 'admin-ops'
                break

        if not team:
            if 'devops' in name:
                team = 'devops'
            elif 'human ops' in name or 'human-ops' in name:
                team = 'human-ops'
            elif 'to leads' in name:
                team = 'ceo-decisions'
            elif 'admin ops' in name or 'admin-ops' in name:
                team = 'admin-ops'

        is_escalation = (
            'escalat' in name
            or 'raise to admin' in name
            or 'human ops' in name
            or 'human-ops' in name
            or 'raise to leads' in name
        )
        if not is_escalation or not team:
            continue

        update_fields = _seed_automatic_blocker_guidance(transition)
        if not has_blocker_pf:
            pfs.insert(0, {
                'type': 'create_blocker_task',
                'team': team,
                'workflow': 'simple',
                'priority': 'urgent',
                'source': SOURCE,
            })
            transition.post_functions = pfs
            update_fields.append('post_functions')
        if update_fields:
            transition.save(update_fields=list(dict.fromkeys(update_fields + ['updated_at'])))


def unseed_assigned_blocker_post_functions(apps, schema_editor):
    Transition = apps.get_model('tasks', 'WorkflowTransition')
    for transition in Transition.objects.filter(to_status='blocked'):
        pfs = _post_functions(transition.post_functions)
        seeded = [
            pf for pf in pfs
            if pf.get('type') == 'create_blocker_task' and pf.get('source') == SOURCE
        ]
        if not seeded:
            continue
        team = str(seeded[0].get('team') or 'admin-ops').strip().lower()
        filtered = [
            pf for pf in pfs
            if not (pf.get('type') == 'create_blocker_task' and pf.get('source') == SOURCE)
        ]
        transition.post_functions = filtered
        transition.description = _restore_manual_blocker_guidance(
            transition.description,
            team,
        )
        articles = []
        for article in _dict_list(transition.knowledge_articles):
            if article.get('title') == 'Automatic assigned blocker child (PLAT-4215)':
                continue
            article = dict(article)
            if 'content' in article:
                article['content'] = _restore_manual_blocker_guidance(
                    article.get('content'),
                    team,
                )
            articles.append(article)
        transition.knowledge_articles = articles
        transition.save(update_fields=[
            'post_functions', 'description', 'knowledge_articles', 'updated_at',
        ])


CREATE_CONSTRAINTS_SQL = r"""
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

CREATE OR REPLACE FUNCTION tasks_check_blocked_item_constraint()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    dependent_id bigint;
    changed_id bigint;
BEGIN
    changed_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;

    IF TG_OP <> 'DELETE' THEN
        PERFORM tasks_assert_blocked_has_owned_blocker(NEW.id);
    END IF;

    FOR dependent_id IN
        SELECT CASE
            WHEN link.link_type = 'blocks' THEN link.target_item_id
            ELSE link.source_item_id
        END
        FROM tasks_item_link link
        WHERE (link.link_type = 'blocks' AND link.source_item_id = changed_id)
           OR (link.link_type IN ('is_blocked_by', 'blocked_by') AND link.target_item_id = changed_id)
    LOOP
        PERFORM tasks_assert_blocked_has_owned_blocker(dependent_id);
    END LOOP;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION tasks_check_blocked_link_constraint()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM tasks_assert_blocked_has_owned_blocker(OLD.source_item_id);
        PERFORM tasks_assert_blocked_has_owned_blocker(OLD.target_item_id);
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        PERFORM tasks_assert_blocked_has_owned_blocker(NEW.source_item_id);
        PERFORM tasks_assert_blocked_has_owned_blocker(NEW.target_item_id);
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS tasks_blocked_owned_blocker_item ON tasks_task;
CREATE CONSTRAINT TRIGGER tasks_blocked_owned_blocker_item
AFTER INSERT OR UPDATE OR DELETE ON tasks_task
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION tasks_check_blocked_item_constraint();

DROP TRIGGER IF EXISTS tasks_blocked_owned_blocker_link ON tasks_item_link;
CREATE CONSTRAINT TRIGGER tasks_blocked_owned_blocker_link
AFTER INSERT OR UPDATE OR DELETE ON tasks_item_link
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION tasks_check_blocked_link_constraint();
"""


DROP_CONSTRAINTS_SQL = r"""
DROP TRIGGER IF EXISTS tasks_blocked_owned_blocker_link ON tasks_item_link;
DROP TRIGGER IF EXISTS tasks_blocked_owned_blocker_item ON tasks_task;
DROP FUNCTION IF EXISTS tasks_check_blocked_link_constraint();
DROP FUNCTION IF EXISTS tasks_check_blocked_item_constraint();
DROP FUNCTION IF EXISTS tasks_assert_blocked_has_owned_blocker(bigint);
"""


def install_database_constraints(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(CREATE_CONSTRAINTS_SQL)
        # CREATE CONSTRAINT TRIGGER is not retroactive: validate the whole
        # pre-existing blocked table before this migration may commit. A restored
        # or drifted database with a naked block must fail closed at install time.
        cursor.execute(
            "SELECT tasks_assert_blocked_has_owned_blocker(id) "
            "FROM tasks_task WHERE lower(status) = 'blocked'"
        )


def remove_database_constraints(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DROP_CONSTRAINTS_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0099_routing_authority_review_fences'),
    ]

    operations = [
        migrations.RunPython(
            seed_assigned_blocker_post_functions,
            unseed_assigned_blocker_post_functions,
        ),
        migrations.RunPython(
            install_database_constraints,
            remove_database_constraints,
        ),
    ]
