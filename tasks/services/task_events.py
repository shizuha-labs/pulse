"""PLS-171 — TaskEvent delta-log emission helpers.

Centralizes appends to the ``TaskEvent`` log so the signal handlers stay thin and
the payload shape is consistent. Emission is **best-effort and MUST NEVER break
the underlying mutation** — every helper swallows its own errors, because a
failed delta-log append must not 500 an item save / comment / link change.
"""
import logging

logger = logging.getLogger(__name__)


def _item_org_id(item):
    """org_id for an item (None for a personal item or if the project can't be
    resolved). Best-effort — never raises."""
    if not getattr(item, 'project_id', None):
        return None
    try:
        project = getattr(item, 'project', None)
        return getattr(project, 'org_id', None) if project else None
    except Exception:
        return None


def record_item_event(item, kind, actor_user_id=None, payload=None):
    """Append a TaskEvent for an Item mutation. Never raises."""
    try:
        from tasks.models import TaskEvent
        TaskEvent.objects.create(
            kind=kind,
            org_id=_item_org_id(item),
            project_id=getattr(item, 'project_id', None),
            item_id=getattr(item, 'pk', None),
            item_key=getattr(item, 'item_key', '') or '',
            actor_user_id=actor_user_id,
            payload=payload or {},
        )
    except Exception:
        logger.exception('PLS-171: TaskEvent emit failed for item (non-fatal)')


def record_comment_event(comment, actor_user_id=None):
    """Append a comment_added TaskEvent, scoped to the comment's item."""
    try:
        from tasks.models import TaskEvent
        actor = actor_user_id if actor_user_id is not None else getattr(comment, 'author_id', None)
        record_item_event(
            comment.item,
            TaskEvent.KIND_COMMENT_ADDED,
            actor_user_id=actor,
            payload={'comment_id': str(comment.pk)},
        )
    except Exception:
        logger.exception('PLS-171: TaskEvent emit failed for comment (non-fatal)')


def record_link_event(link, actor_user_id=None):
    """Append a link_changed TaskEvent, scoped to the link's source item."""
    try:
        from tasks.models import TaskEvent
        record_item_event(
            link.source_item,
            TaskEvent.KIND_LINK_CHANGED,
            actor_user_id=actor_user_id,
            payload={
                'link_type': getattr(link, 'link_type', ''),
                'target_item_id': getattr(link, 'target_item_id', None),
            },
        )
    except Exception:
        logger.exception('PLS-171: TaskEvent emit failed for link (non-fatal)')
