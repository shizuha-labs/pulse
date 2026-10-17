"""PLAT-10558: surface a seat that cannot write the linked repo.

The transition itself is not denied. A definitive read-only or missing
collaborator result is commented and the item is routed to the devops grant
queue. Network and token failures stay fail-open so a probe outage cannot
stall every in_progress transition.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

_GRANT_GROUP = 'devops'
_WRITE = {'write', 'admin', 'owner'}


def repo_from_item(item) -> str:
    meta = getattr(item, 'metadata', None) or {}
    if not isinstance(meta, dict):
        meta = {}
    for obs in meta.get('origin_observations') or []:
        if isinstance(obs, dict) and obs.get('repo'):
            return str(obs['repo'])
    for pr in meta.get('pull_requests') or []:
        if not isinstance(pr, dict):
            continue
        repo = str(pr.get('repo') or '')
        if '/' in repo:
            return repo
        url = str(pr.get('url') or '')
        match = re.search(r'origin\.shizuha\.com/([^/\s]+)/([^/\s]+)/', url)
        if match:
            return f'{match.group(1)}/{match.group(2)}'
    return ''


def forgejo_login(email: str) -> str:
    local = str(email or '').split('@', 1)[0].strip()
    return local


def permission_blocks_write(permission: str | None, *, status_code: int) -> bool:
    """True only for a definitive non-write answer. Unknown stays open."""
    if status_code == 404:
        return True
    if status_code != 200:
        return False
    return str(permission or '').strip().lower() not in _WRITE


def probe_assignee_repo_write(item_pk: int) -> None:
    # The suite and every other process that happens to hold a Forgejo token
    # must not call Origin or reroute items. Production turns this on.
    if os.environ.get('PLAT_10558_REPO_WRITE_PROBE') != '1':
        return
    from ..models import Item

    try:
        item = Item.objects.get(pk=item_pk)
    except Item.DoesNotExist:
        return
    if (item.status or '') != 'in_progress':
        return
    if (item.assignment_group or '') == _GRANT_GROUP:
        return
    repo = repo_from_item(item)
    email = str(item.assignee_email or '').strip()
    login = forgejo_login(email)
    if not repo or not login or '/' not in repo:
        return
    token = (
        os.environ.get('FORGEJO_TOKEN')
        or os.environ.get('ORIGIN_TOKEN')
        or ''
    )
    if not token:
        return
    owner, name = repo.split('/', 1)
    url = (
        f'https://origin.shizuha.com/api/v1/repos/{owner}/{name}'
        f'/collaborators/{login}'
    )
    try:
        import requests
        response = requests.get(
            url,
            headers={'Authorization': f'token {token}', 'Accept': 'application/json'},
            timeout=8,
        )
    except Exception:
        logger.warning('PLAT-10558 repo-write probe failed open for %s', item_pk, exc_info=True)
        return
    permission = ''
    if response.status_code == 200:
        try:
            permission = str((response.json() or {}).get('permission') or '')
        except Exception:
            return
    if not permission_blocks_write(permission, status_code=response.status_code):
        return
    _surface_block(item, repo=repo, login=login, status_code=response.status_code, permission=permission)


def _surface_block(item, *, repo: str, login: str, status_code: int, permission: str) -> None:
    from .mechanical_comments import MechanicalCommentFactory
    from ..models import Comment

    item.assignment_group = _GRANT_GROUP
    meta = dict(item.metadata or {})
    meta['seat_write_blocked'] = {
        'repo': repo,
        'login': login,
        'status_code': status_code,
        'permission': permission or 'absent',
    }
    item.metadata = meta
    item.save(update_fields=['assignment_group', 'metadata', 'updated_at'])
    MechanicalCommentFactory.create(
        item=item,
        origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
        origin_event='assignment.repo_write_blocked',
        template_version=1,
        structured_payload={
            'login': login,
            'repo': repo,
            'status_code': str(status_code),
            'permission': permission or 'absent',
            'grant_group': _GRANT_GROUP,
        },
        origin_event_id=f'repo-write:{item.pk}:{repo}:{login}:{status_code}',
    )
