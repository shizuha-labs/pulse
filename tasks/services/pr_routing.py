"""PLS-514: turn a green/open Origin PR into routed work for a team.

The gap this closes
-------------------
Review and merge routing happens exclusively through *task* workflow
transitions.  A pull request therefore becomes actionable only if its author
both (a) has a Pulse task and (b) can reach ``Submit for ... Review`` on that
task's workflow.  Neither is guaranteed:

* an on-call fix answering an alert has **no task at all** — alerts are DM-only
  by design and never create Pulse items (shizuha-tasks#140);
* a task on ``Simple Workflow`` has **no transition to in_review** at all, so
  the author cannot route it however diligent they are (computax#4).

For those the PR is invisible to everyone except its author: a PR with no
reviewer looks identical to one that is progressing.  This module sources the
work item from the **PR** rather than from the author's task, which is the only
framing that covers the two cases above.

Safety
------
This creates Pulse tasks from an external webhook, so the failure mode is a
task flood (there are >200 open PRs across the gated repos).  Three guards, all
fail-closed:

* ``PULSE_PR_ROUTING_ENABLED`` — master switch, **default OFF**;
* ``PULSE_PR_ROUTING_REPOS`` — explicit ``owner/repo`` allowlist, empty = none;
* ``MAX_ITEMS_PER_EVENT`` — hard cap per webhook invocation.

Deduplication is on ``(repo, number)``, so redelivery, ``synchronize`` storms
and check re-runs all refresh one item instead of creating another.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from django.utils import timezone

from ..models import Item

logger = logging.getLogger(__name__)

# One routing item per PR, ever — the dedupe identity.
_META_REPO = 'pr_routing_repo'
_META_NUMBER = 'pr_routing_number'
_META_KIND = 'pr_routing_kind'

KIND_REVIEW = 'review'
KIND_MERGE = 'merge'

# A single webhook delivery describes one PR, so >1 item means a bug upstream.
MAX_ITEMS_PER_EVENT = 1

_TERMINAL = {
    'completed', 'done', 'closed', 'cancelled', 'canceled', 'resolved',
    'rejected', 'duplicate', 'wont_fix', 'merged', 'failed', 'expired',
    'deferred',
}


def _enabled() -> bool:
    return (os.environ.get('PULSE_PR_ROUTING_ENABLED', '') or '').strip().lower() in {
        '1', 'true', 'yes', 'on',
    }


def _allowed_repos() -> set[str]:
    raw = os.environ.get('PULSE_PR_ROUTING_REPOS', '') or ''
    return {r.strip().lower() for r in raw.replace(',', ' ').split() if r.strip()}


def _team_for(kind: str) -> str:
    if kind == KIND_MERGE:
        return (os.environ.get('PULSE_PR_ROUTING_MERGE_TEAM', 'merge') or 'merge').strip()
    return (os.environ.get('PULSE_PR_ROUTING_REVIEW_TEAM', 'review') or 'review').strip()


def _pr_already_has_task(url: str, repo: str, number: int) -> bool:
    """True when some NON-routing task already links this PR.

    @hiro's second negative control (PLAT-5050): this must not mint an item for
    a PR that already has a task, or every task-first PR — the overwhelming
    majority — acquires a duplicate. The point is to cover PRs the author
    *cannot* route, not to shadow the ones they can.

    Deliberately skips our own routing items (they carry ``pr_routing_repo``),
    so a refresh cannot read its own artifact as "already covered" and decline.
    """
    url = (url or '').rstrip('/')
    for item in (
        Item.objects
        .filter(metadata__pull_requests__isnull=False)
        .only('id', 'metadata')
        .iterator()
    ):
        meta = item.metadata or {}
        if meta.get(_META_REPO):
            continue  # one of ours — not evidence of author-side routing
        for entry in (meta.get('pull_requests') or []):
            if url and (entry.get('url') or '').rstrip('/') == url:
                return True
            if (str(entry.get('repo') or '').strip().lower() == repo
                    and str(entry.get('number') or '') == str(number)):
                return True
    return False


def _find_existing(repo: str, number: int) -> Optional[Item]:
    """Existing routing item for this PR, terminal or not.

    Terminal items are included deliberately: a PR reopened after its item was
    resolved must revive that item rather than mint a second one.
    """
    return (
        Item.objects
        .filter(metadata__pr_routing_repo=repo, metadata__pr_routing_number=number)
        .order_by('created_at')
        .first()
    )


def resolve_pr_routing_item(repo: str, number: int, *, reason: str) -> Optional[Item]:
    """Close the routing item once the PR is merged or closed."""
    if not repo or not number:
        return None
    existing = _find_existing(repo, int(number))
    if existing is None or (existing.status or '') in _TERMINAL:
        return None
    existing.status = 'done'
    existing.metadata = {
        **(existing.metadata or {}),
        'pr_routing_resolved_reason': reason,
        'pr_routing_resolved_at': timezone.now().isoformat(),
    }
    existing.save(update_fields=['status', 'metadata', 'updated_at'])
    logger.info('PLS-514: resolved PR routing item %s (%s#%s, %s)',
                existing.item_key or existing.pk, repo, number, reason)
    return existing


def ensure_pr_routing_item(
    *,
    repo: str,
    number: int,
    url: str,
    title: str,
    kind: str = KIND_REVIEW,
    draft: bool = False,
    state: str = 'open',
    merged: bool = False,
    project_id=None,
) -> Optional[Item]:
    """Ensure exactly one open work item exists for this PR.

    Returns the item, or ``None`` when a guard declined (disabled, repo not
    allowlisted, draft, or PR no longer open). Never raises into the webhook.
    """
    if not _enabled():
        return None
    repo = (repo or '').strip().lower()
    if not repo or not number:
        return None
    if repo not in _allowed_repos():
        # Not an error: the allowlist is how this is rolled out repo by repo.
        return None

    # A merged/closed PR resolves rather than routes.
    if merged or (state or '').lower() == 'closed':
        return resolve_pr_routing_item(repo, int(number),
                                       reason='merged' if merged else 'closed')
    if draft:
        # A draft is explicitly "not ready for a human"; routing it is noise.
        return None

    kind = kind if kind in {KIND_REVIEW, KIND_MERGE} else KIND_REVIEW
    team = _team_for(kind)
    number = int(number)
    existing = _find_existing(repo, number)

    # @hiro's negative control: a PR the author CAN route already has a task and
    # its own path to `Submit for Review`. Minting here would duplicate it. Only
    # applies on creation — an item we already made keeps being refreshed even
    # if a task is linked later, so the two records converge rather than fork.
    if existing is None and _pr_already_has_task(url, repo, number):
        return None

    verb = 'Merge' if kind == KIND_MERGE else 'First-pass review'
    item_title = f'[{kind}] {verb}: {repo}#{number} — {(title or "").strip()[:120]}'
    description = (
        f'Automatic routing item (PLS-514).\n\n'
        f'**PR:** {url}\n'
        f'**Repo:** `{repo}`  **Number:** `#{number}`\n\n'
        f'This item exists because review and merge routing otherwise happens only through a '
        f'task workflow transition, which leaves a PR unroutable when it has no linked task, or '
        f'when its task sits on a workflow with no path to `in_review`. It is sourced from the '
        f'PR itself so neither case can hide it.\n\n'
        f'It auto-resolves when the PR merges or closes. Do not close it by hand while the PR is '
        f'still open — it will be recreated on the next PR event.\n'
    )

    if existing is not None:
        # Refresh in place. Reviving a resolved item is deliberate (PR reopened).
        revived = (existing.status or '') in _TERMINAL
        existing.title = item_title
        existing.description = description
        existing.assignment_group = team
        if revived:
            existing.status = 'open'
            existing.assignee_id = None
            existing.assignee_email = None
        existing.metadata = {
            **(existing.metadata or {}),
            _META_REPO: repo,
            _META_NUMBER: number,
            _META_KIND: kind,
            'pr_routing_url': url,
            'pr_routing_last_seen_at': timezone.now().isoformat(),
        }
        existing.save(update_fields=[
            'title', 'description', 'assignment_group', 'status',
            'assignee_id', 'assignee_email', 'metadata', 'updated_at',
        ])
        return existing

    item = Item.objects.create(
        title=item_title,
        description=description,
        status='open',
        priority='high',
        mode='task',
        workflow_name='autonomous-dev',
        assignment_group=team,
        project_id=project_id,
        metadata={
            _META_REPO: repo,
            _META_NUMBER: number,
            _META_KIND: kind,
            'pr_routing_url': url,
            'pr_routing_created_at': timezone.now().isoformat(),
        },
    )
    logger.info('PLS-514: created %s routing item %s for %s#%s -> team=%s',
                kind, item.item_key or item.pk, repo, number, team)
    return item
