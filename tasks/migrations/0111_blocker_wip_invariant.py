"""Keep runnable dependency roots out of inactive parking states."""

from django.db import migrations
from django.db.models import Q


TERMINAL = (
    'completed', 'done', 'closed', 'cancelled', 'canceled', 'resolved',
    'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired',
    'deferred', 'silenced', 'applied', 'change_rejected',
    'rfc_approved', 'rfc_implementation_linked', 'rfc_validation',
)


def activate_existing_backlog_blockers(apps, schema_editor):
    Item = apps.get_model('tasks', 'Item')
    ItemLink = apps.get_model('tasks', 'ItemLink')
    Status = apps.get_model('tasks', 'Status')
    Workflow = apps.get_model('tasks', 'Workflow')
    WorkflowStatus = apps.get_model('tasks', 'WorkflowStatus')

    parked_slugs = set(
        Status.objects.filter(
            category__in=('backlog', 'scheduled'),
        ).values_list('slug', flat=True)
    )
    if not parked_slugs:
        return

    active_items = Item.objects.filter(
        resolution__isnull=True,
        is_archived=False,
    ).exclude(status__in=TERMINAL)
    active_ids = active_items.values('pk')
    blocker_ids = set(
        ItemLink.objects.filter(
            link_type='blocks', target_item_id__in=active_ids,
        ).values_list('source_item_id', flat=True)
    )
    blocker_ids.update(
        ItemLink.objects.filter(
            link_type__in=('is_blocked_by', 'blocked_by'),
            source_item_id__in=active_ids,
        ).values_list('target_item_id', flat=True)
    )

    fallback = (
        Status.objects.filter(slug='open').first()
        or Status.objects.filter(category='todo').order_by('position').first()
        or Status.objects.filter(category='in_progress').exclude(
            slug='blocked',
        ).order_by('position').first()
    )
    for item in Item.objects.filter(
        pk__in=blocker_ids,
        status__in=parked_slugs,
        resolution__isnull=True,
        is_archived=False,
    ).iterator():
        wf = None
        if item.workflow_name:
            value = str(item.workflow_name)
            variants = {
                value.lower(),
                value.lower().replace('-', '_'),
                value.lower().replace('_', '-'),
                '-'.join(value.lower().split()),
                '_'.join(value.lower().split()),
            }
            wf = Workflow.objects.filter(
                Q(name__iexact=value) | Q(slug__in=variants)
            ).first()
        target = getattr(wf, 'initial_status', None) if wf else None
        if target and (
            target.category in {'backlog', 'done', 'scheduled'}
            or target.slug == 'blocked'
        ):
            target = None
        if wf and target is None:
            target = (
                Status.objects.filter(
                    workflow_memberships__workflow=wf,
                    category='todo',
                ).order_by('workflow_memberships__position').first()
                or Status.objects.filter(
                    workflow_memberships__workflow=wf,
                    category='in_progress',
                ).exclude(slug='blocked').order_by(
                    'workflow_memberships__position',
                ).first()
            )
        target = target or fallback
        if target:
            Item.objects.filter(pk=item.pk).update(
                status=target.slug,
                workflow_status=target.slug,
            )

    # A completed/deferred blocker is not runnable work. If it was the final
    # live blocker but its dependency child is still `blocked`, restore the
    # child in this migration before the deferred constraint is installed.
    # Multi-blocker children remain blocked while any non-releasing blocker
    # remains. Manual/operator blocks explicitly recorded as having no active
    # blocker are unrelated to the stale terminal link and are left alone.
    releasing = set(TERMINAL)
    blocked_items = Item.objects.filter(
        status='blocked',
        resolution__isnull=True,
        is_archived=False,
    )
    for dependent in blocked_items.iterator():
        metadata = dependent.metadata if isinstance(dependent.metadata, dict) else {}
        pre_block = metadata.get('pre_block_state')
        if isinstance(pre_block, dict) and pre_block.get('had_active_blocker') is False:
            continue
        blockers = []
        for link in ItemLink.objects.filter(
            Q(target_item_id=dependent.pk, link_type='blocks')
            | Q(
                source_item_id=dependent.pk,
                link_type__in=('is_blocked_by', 'blocked_by'),
            )
        ):
            blocker_id = (
                link.source_item_id
                if link.link_type == 'blocks'
                else link.target_item_id
            )
            blocker = Item.objects.filter(pk=blocker_id).first()
            if blocker:
                blockers.append(blocker)
        if not blockers or any(
            str(blocker.status or '').lower().replace('-', '_') not in releasing
            for blocker in blockers
        ):
            continue
        raw_resume = pre_block.get('status') if isinstance(pre_block, dict) else None
        resume = str(raw_resume or '').strip().lower()
        if (
            not resume
            or resume == 'blocked'
            or resume in releasing
            or resume in parked_slugs
            or not Status.objects.filter(slug=resume).exists()
        ):
            resume = fallback.slug if fallback else ''
        if resume:
            Item.objects.filter(pk=dependent.pk).update(
                status=resume,
                workflow_status=resume,
            )


POSTGRES_SQL = r"""
CREATE OR REPLACE FUNCTION tasks_assert_blocker_in_wip(p_item_id bigint)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        WITH dependency_edges AS (
            SELECT
                link.id AS link_id,
                blocker.id AS blocker_id,
                dependent.id AS dependent_id,
                blocker.resolution_id AS blocker_resolution_id,
                blocker.is_archived AS blocker_is_archived,
                dependent.resolution_id AS dependent_resolution_id,
                dependent.is_archived AS dependent_is_archived,
                lower(replace(trim(coalesce(blocker.status, '')), '-', '_'))
                    AS blocker_status,
                lower(replace(trim(coalesce(dependent.status, '')), '-', '_'))
                    AS dependent_status,
                blocker_status.category AS blocker_category,
                coalesce(
                    dependent.metadata #>> '{pre_block_state,had_active_blocker}',
                    'true'
                ) AS had_active_blocker
            FROM tasks_item_link link
            JOIN tasks_task blocker
              ON blocker.id = CASE
                  WHEN link.link_type = 'blocks' THEN link.source_item_id
                  WHEN link.link_type IN ('is_blocked_by', 'blocked_by')
                      THEN link.target_item_id
                  ELSE NULL
              END
            JOIN tasks_task dependent
              ON dependent.id = CASE
                  WHEN link.link_type = 'blocks' THEN link.target_item_id
                  WHEN link.link_type IN ('is_blocked_by', 'blocked_by')
                      THEN link.source_item_id
                  ELSE NULL
              END
            LEFT JOIN tasks_status blocker_status
              ON lower(blocker_status.slug) = lower(blocker.status)
            WHERE link.link_type IN ('blocks', 'is_blocked_by', 'blocked_by')
        )
        SELECT 1
        FROM dependency_edges edge
        WHERE (edge.blocker_id = p_item_id OR edge.dependent_id = p_item_id)
          AND edge.dependent_resolution_id IS NULL
          AND NOT edge.dependent_is_archived
          AND edge.dependent_status NOT IN (
              'completed', 'done', 'closed', 'cancelled', 'canceled',
              'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
              'failed', 'expired', 'deferred', 'silenced', 'applied',
              'change_rejected', 'rfc_approved',
              'rfc_implementation_linked', 'rfc_validation'
          )
          AND (
              edge.blocker_status IN ('backlog', 'scheduled')
              OR edge.blocker_category IN ('backlog', 'scheduled')
              OR (
                  edge.had_active_blocker <> 'false'
                  AND (
                      edge.blocker_resolution_id IS NOT NULL
                      OR edge.blocker_is_archived
                      OR edge.blocker_category = 'done'
                      OR edge.blocker_status IN (
                          'completed', 'done', 'closed', 'cancelled', 'canceled',
                          'resolved', 'rejected', 'duplicate', 'wont_fix', 'merged',
                          'failed', 'expired', 'deferred', 'silenced', 'applied',
                          'change_rejected', 'rfc_approved',
                          'rfc_implementation_linked', 'rfc_validation'
                      )
                  )
                  AND edge.dependent_status = 'blocked'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM dependency_edges other
                      WHERE other.dependent_id = edge.dependent_id
                        AND other.link_id <> edge.link_id
                        AND other.blocker_resolution_id IS NULL
                        AND NOT other.blocker_is_archived
                        AND other.blocker_status NOT IN (
                            'completed', 'done', 'closed', 'cancelled',
                            'canceled', 'resolved', 'rejected', 'duplicate',
                            'wont_fix', 'merged', 'failed', 'expired',
                            'deferred', 'silenced', 'applied',
                            'change_rejected', 'rfc_approved',
                            'rfc_implementation_linked', 'rfc_validation',
                            'backlog', 'scheduled'
                        )
                        AND coalesce(other.blocker_category, '')
                            NOT IN ('backlog', 'scheduled', 'done')
                  )
              )
          )
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = format(
                'active_blocker_must_remain_wip: item_id=%s',
                p_item_id
            ),
            DETAIL = (
                'A live dependency root cannot be parked outside WIP, and a '
                'releasing transition must restore its final blocked dependent.'
            );
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION tasks_enforce_blocker_in_wip_item()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM tasks_assert_blocker_in_wip(NEW.id);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION tasks_enforce_blocker_in_wip_link()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM tasks_assert_blocker_in_wip(NEW.source_item_id);
    PERFORM tasks_assert_blocker_in_wip(NEW.target_item_id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tasks_blocker_in_wip_item_ct ON tasks_task;
CREATE CONSTRAINT TRIGGER tasks_blocker_in_wip_item_ct
AFTER INSERT OR UPDATE OF status, resolution_id, is_archived ON tasks_task
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION tasks_enforce_blocker_in_wip_item();

DROP TRIGGER IF EXISTS tasks_blocker_in_wip_link_ct ON tasks_item_link;
CREATE CONSTRAINT TRIGGER tasks_blocker_in_wip_link_ct
AFTER INSERT OR UPDATE OF source_item_id, target_item_id, link_type ON tasks_item_link
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION tasks_enforce_blocker_in_wip_link();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS tasks_blocker_in_wip_link_ct ON tasks_item_link;
DROP TRIGGER IF EXISTS tasks_blocker_in_wip_item_ct ON tasks_task;
DROP FUNCTION IF EXISTS tasks_enforce_blocker_in_wip_link();
DROP FUNCTION IF EXISTS tasks_enforce_blocker_in_wip_item();
DROP FUNCTION IF EXISTS tasks_assert_blocker_in_wip(bigint);
"""


def install_postgres_guard(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(POSTGRES_SQL)


def remove_postgres_guard(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ('tasks', '0110_uncap_active_wip'),
    ]

    operations = [
        migrations.RunPython(
            activate_existing_backlog_blockers,
            migrations.RunPython.noop,
        ),
        migrations.RunPython(install_postgres_guard, remove_postgres_guard),
    ]
