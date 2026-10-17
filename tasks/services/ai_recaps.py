"""Bounded, citation-backed Pulse recap generation (PLS-307)."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Iterable

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import AiGenerationRun, Comment, Item, ItemActivity, Project, Sprint
from .permissions import PermissionService

PROMPT_TEMPLATE_VERSION = 'pulse-recap-v1'
DEFAULT_MODEL = 'deterministic-pulse-recap-v1'
MAX_WINDOW_DAYS = 31
MAX_RUNS_PER_HOUR = 10

_TERMINAL_STATUSES = {'completed', 'done', 'resolved', 'merged', 'closed'}


class RecapPermissionDenied(PermissionError):
    """Raised when a caller requests a recap outside their readable project scope."""


def _user_id(user):
    value = getattr(user, 'id', None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _user_email(user):
    return (getattr(user, 'email', '') or '').strip().lower()


def _json_default(value):
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def _citation(ref: str, label: str, kind: str, item_key: str | None = None) -> dict:
    out = {'ref': ref, 'label': label, 'kind': kind}
    if item_key:
        out['item_key'] = item_key
    return out


def _normalize_window(start, end):
    if start is None or end is None:
        raise ValidationError('source_window_start and source_window_end are required')
    if end <= start:
        raise ValidationError('source_window_end must be after source_window_start')
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise ValidationError(f'recap windows are capped at {MAX_WINDOW_DAYS} days')
    return start, end


def ensure_can_access_project(user, project: Project) -> None:
    accessible = PermissionService.get_user_projects(user=user).filter(id=project.id).exists()
    if not accessible:
        raise RecapPermissionDenied('Project is outside the caller readable scope')


def _collect_sources(project: Project, start, end, sprint: Sprint | None = None) -> dict:
    item_window = (
        Q(created_at__gte=start, created_at__lte=end)
        | Q(updated_at__gte=start, updated_at__lte=end)
        | Q(completed_at__gte=start, completed_at__lte=end)
    )
    base_item_qs = Item.objects.filter(project=project)
    if sprint is not None:
        base_item_qs = base_item_qs.filter(sprint=sprint)

    comment_item_ids = Comment.objects.filter(
        item__project=project,
        created_at__gte=start,
        created_at__lte=end,
    )
    activity_item_ids = ItemActivity.objects.filter(
        item__project=project,
        created_at__gte=start,
        created_at__lte=end,
    )
    if sprint is not None:
        comment_item_ids = comment_item_ids.filter(item__sprint=sprint)
        activity_item_ids = activity_item_ids.filter(item__sprint=sprint)

    item_qs = base_item_qs.filter(
        item_window
        | Q(id__in=comment_item_ids.values('item_id'))
        | Q(id__in=activity_item_ids.values('item_id'))
    )
    items = list(item_qs.order_by('priority', 'item_key', 'id')[:200])
    item_ids = [item.id for item in items]
    comments = list(Comment.objects.filter(
        item_id__in=item_ids,
        created_at__gte=start,
        created_at__lte=end,
    ).order_by('created_at')[:500])
    activities = list(ItemActivity.objects.filter(
        item_id__in=item_ids,
        created_at__gte=start,
        created_at__lte=end,
    ).order_by('created_at')[:500])
    return {'items': items, 'comments': comments, 'activities': activities}


def _snapshot_hash(sources: dict) -> str:
    payload = {
        'items': [
            {
                'id': item.id,
                'key': item.item_key,
                'title': item.title,
                'status': item.status,
                'priority': item.priority,
                'updated_at': item.updated_at,
                'completed_at': item.completed_at,
                'metadata': item.metadata if isinstance(getattr(item, 'metadata', None), dict) else {},
            }
            for item in sources['items']
        ],
        'comments': [
            {'id': c.id, 'item_id': c.item_id, 'created_at': c.created_at, 'content_hash': hashlib.sha256(c.content.encode()).hexdigest()}
            for c in sources['comments']
        ],
        'activities': [
            {'id': str(a.id), 'item_id': a.item_id, 'action': a.action, 'field': a.field_name, 'created_at': a.created_at}
            for a in sources['activities']
        ],
    }
    canonical = json.dumps(payload, default=_json_default, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _item_citations(item: Item, comments_by_item: dict[int, list[Comment]], activities_by_item: dict[int, list[ItemActivity]]) -> list[dict]:
    key = item.item_key or f'#{item.id}'
    citations = [_citation(f'item:{item.id}', key, 'task', key)]
    first_comment = (comments_by_item.get(item.id) or [None])[0]
    if first_comment:
        citations.append(_citation(f'comment:{first_comment.id}', f'comment on {key}', 'comment', key))
    first_activity = (activities_by_item.get(item.id) or [None])[0]
    if first_activity:
        citations.append(_citation(f'activity:{first_activity.id}', f'{first_activity.action} on {key}', 'activity', key))
    metadata = item.metadata if isinstance(getattr(item, 'metadata', None), dict) else {}
    for pr in (metadata.get('pull_requests') or [])[:3]:
        if isinstance(pr, dict) and pr.get('url'):
            citations.append({
                'ref': f"pr:{pr['url']}",
                'label': pr.get('title') or pr.get('url'),
                'kind': 'pull_request',
                'item_key': key,
                'url': pr.get('url'),
            })
    return citations


def _group_by_item(rows: Iterable) -> dict[int, list]:
    grouped: dict[int, list] = {}
    for row in rows:
        grouped.setdefault(row.item_id, []).append(row)
    return grouped


def _render_output(run: AiGenerationRun, sources: dict) -> tuple[dict, list[dict]]:
    items = sources['items']
    comments_by_item = _group_by_item(sources['comments'])
    activities_by_item = _group_by_item(sources['activities'])
    bullets = []
    flat_citations = []

    def add_bullet(category, text, citations):
        citation_refs = [c['ref'] for c in citations]
        bullets.append({'category': category, 'text': text, 'citations': citation_refs})
        flat_citations.extend(citations)

    completed = [i for i in items if i.status in _TERMINAL_STATUSES or i.completed_at]
    active = [i for i in items if i.status not in _TERMINAL_STATUSES]
    risks = [i for i in active if i.status == 'blocked' or i.priority in ('urgent', 'high')]

    for item in completed[:5]:
        key = item.item_key or f'#{item.id}'
        add_bullet('progress', f'Completed or advanced {key}: {item.title}', _item_citations(item, comments_by_item, activities_by_item))

    for item in active[:5]:
        key = item.item_key or f'#{item.id}'
        add_bullet('active', f'Active work remains on {key}: {item.title} ({item.status})', _item_citations(item, comments_by_item, activities_by_item))

    for item in risks[:5]:
        key = item.item_key or f'#{item.id}'
        add_bullet('risk', f'Watch {key}: {item.title} is {item.priority}/{item.status}', _item_citations(item, comments_by_item, activities_by_item))

    if not bullets:
        add_bullet('empty', 'No Pulse task, comment, or activity evidence changed in this bounded window.', [])

    deduped = {c['ref']: c for c in flat_citations}
    return {
        'title': f'{run.project.key} {run.get_type_display()} ({run.source_window_start.date()} → {run.source_window_end.date()})',
        'summary_format': 'citation-backed-bullets-v1',
        'generated_at': timezone.now().isoformat(),
        'bullets': bullets,
        'source_window': {
            'start': run.source_window_start.isoformat(),
            'end': run.source_window_end.isoformat(),
        },
    }, list(deduped.values())


def create_recap_run(*, user, project: Project, source_window_start, source_window_end, recap_type='project', sprint: Sprint | None = None, model=DEFAULT_MODEL, prompt_template_version=PROMPT_TEMPLATE_VERSION, client_id='pulse-api') -> AiGenerationRun:
    ensure_can_access_project(user, project)
    source_window_start, source_window_end = _normalize_window(source_window_start, source_window_end)
    if sprint is not None and sprint.project_id != project.id:
        raise ValidationError('sprint must belong to the requested project')

    recent_cutoff = timezone.now() - timedelta(hours=1)
    recent_count = AiGenerationRun.objects.filter(
        project=project,
        created_by_email=_user_email(user),
        created_at__gte=recent_cutoff,
    ).count()
    if recent_count >= MAX_RUNS_PER_HOUR:
        raise ValidationError('recap rate limit exceeded for this project/user')

    run = AiGenerationRun.objects.create(
        org_id=project.org_id,
        project=project,
        sprint=sprint,
        type=recap_type,
        status='queued',
        model=model or DEFAULT_MODEL,
        prompt_template_version=prompt_template_version or PROMPT_TEMPLATE_VERSION,
        source_window_start=source_window_start,
        source_window_end=source_window_end,
        created_by_id=_user_id(user),
        created_by_email=_user_email(user),
        client_id=client_id or 'pulse-api',
    )
    # Production path is asynchronous; tests/local callers with
    # CELERY_TASK_ALWAYS_EAGER get the same durable row updated before return.
    from ..tasks import generate_ai_recap
    async_result = generate_ai_recap.delay(str(run.id))
    if getattr(async_result, 'result', None) is not None:
        run.refresh_from_db()
    return run


def _persist_failed_state(run_id, exc) -> None:
    """Durably record a failed generation in its OWN transaction.

    kei P1 (PLS-307): the generation body below runs inside `transaction.atomic()`.
    Writing `status='failed'` inside that same block and then re-raising rolls the
    write back with the transaction, so the run would be left `queued`/`running`
    with no error despite the failure. Persisting the failure in a SEPARATE, already
    committed-or-fresh transaction is what makes the failed state survive the
    rollback and the Celery retry cycle. Best-effort: never mask the original error.
    """
    try:
        with transaction.atomic():
            run = AiGenerationRun.objects.select_for_update().get(id=run_id)
            run.status = 'failed'
            run.error_message = (str(exc) or exc.__class__.__name__)[:2000]
            run.save(update_fields=['status', 'error_message', 'updated_at'])
    except Exception:  # pragma: no cover - defensive; original exception is re-raised by caller
        import logging
        logging.getLogger(__name__).exception(
            'Failed to persist failed-state for AI recap run %s', run_id)


def generate_recap_run(run_id) -> AiGenerationRun:
    try:
        with transaction.atomic():
            run = AiGenerationRun.objects.select_for_update(of=('self',)).select_related('project', 'sprint').get(id=run_id)
            run.status = 'running'
            run.save(update_fields=['status', 'updated_at'])
            sources = _collect_sources(run.project, run.source_window_start, run.source_window_end, run.sprint)
            output, citations = _render_output(run, sources)
            run.output = output
            run.citations = citations
            run.source_counts = {
                'items': len(sources['items']),
                'comments': len(sources['comments']),
                'activities': len(sources['activities']),
            }
            run.source_snapshot_hash = _snapshot_hash(sources)
            run.status = 'completed'
            run.completed_at = timezone.now()
            run.error_message = ''
            run.save(update_fields=[
                'output', 'citations', 'source_counts', 'source_snapshot_hash',
                'status', 'completed_at', 'error_message', 'updated_at',
            ])
    except Exception as exc:
        # Record the failure in a separate transaction so it is not rolled back
        # with the generation transaction above, then re-raise for Celery retry.
        _persist_failed_state(run_id, exc)
        raise
    return run


@transaction.atomic
def append_recap_feedback(*, run: AiGenerationRun, user, verdict: str, comment: str = '', bullet_index=None) -> AiGenerationRun:
    entry = {
        'verdict': (verdict or '').strip()[:50],
        'comment': (comment or '').strip()[:2000],
        'bullet_index': bullet_index,
        'user_id': _user_id(user),
        'user_email': _user_email(user),
        'created_at': timezone.now().isoformat(),
    }
    feedback = list(run.feedback or [])
    feedback.append(entry)
    run.feedback = feedback
    run.save(update_fields=['feedback', 'updated_at'])
    return run
