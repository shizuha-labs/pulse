"""Write-time admission guardrails for Pulse comments (comment-noise audit, 2026-11-09).

Audit of ``tasks_comment`` on 2026-11-09 (1,090,003 rows):

* 857,132 rows (78.6%) were written by Pulse's own sweeps (``system@pulse`` /
  ``workflow@pulse``); only ~86k were typed USER_AUTHORED.
* 571,349 automation rows repeated a byte-identical body by the same author on
  the same item within 4 hours (647,338 within 24 hours). Each of those rows
  also fanned out into an ``ItemActivity`` ("commented"), a ``TaskEvent``
  (``comment_added``), notifications and outbox rows.
* Agent/human comments were overwhelmingly substantive (p99.9 = 22 comments per
  author per item per hour), but a few loops still produced bursts of 60-70 per
  item-hour and 1,335 per author-hour.

This module is the single policy point for both write paths:

``admit_automation_comment`` (called from ``Comment.save`` for new rows)
    Automation authors cannot append a byte-identical body to the same item
    within ``PULSE_AUTOMATION_COMMENT_DEDUPE_MINUTES`` (default 240). Instead of
    inserting, the caller receives the existing identical row, so callers that
    use the returned ``Comment`` keep working and no post_save fan-out happens.
    Over-long automation bodies are truncated to
    ``PULSE_AUTOMATION_COMMENT_MAX_CHARS`` (default 16000).

``admit_user_comment`` (called from the public comment create API)
    * body longer than ``PULSE_COMMENT_MAX_CHARS`` (default 65536) -> 400; logs
      and large outputs belong in an attachment;
    * same author + item + identical body within
      ``PULSE_COMMENT_DUPLICATE_WINDOW_SECONDS`` (default 600) -> the existing
      comment is returned (idempotent replay, no new row, no notifications);
    * more than ``PULSE_COMMENT_ITEM_HOURLY_LIMIT`` (default 30) comments by one
      author on one item in the trailing hour, or more than
      ``PULSE_COMMENT_AUTHOR_HOURLY_LIMIT`` (default 300) by one author overall
      -> 429. Operator accounts and ``PULSE_COMMENT_RATE_EXEMPT_EMAILS`` are
      exempt from the rate limits.

Every limit is an environment knob; ``0`` disables that guard.

State-marker comments are never deduped: a few sweeps read their own recent
comments back as cooldown/watchdog state (``Auto-failover`` cooldown,
force-pull boundary, PLAT-10519 override watchdog), so suppressing a repeat of
those bodies could weaken an anti-churn guard. They are listed in
``STATE_MARKER_PREFIXES``.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

AUTOMATION_AUTHOR_SUFFIX = '@pulse'
DEFAULT_EXTRA_AUTOMATION_AUTHORS = (
    'system@shizuha.com',
    'qa-automation@shizuha.com',
    'builds-health@shizuha.com',
)

# Bodies that existing code reads back as state within a trailing window.
STATE_MARKER_PREFIXES = (
    'Auto-failover',  # tasks._failover_cooldown_active (PULSE_FAILOVER_COOLDOWN_HOURS)
    'Pulled from the **',  # WorkflowService force-pull boundary
    '🚨 **Critical unassigned override**',  # force-pull boundary + PLAT-10519 watchdog
    'Direct assignee override used during task',  # PLAT-10519 watchdog
)

# Typed (MechanicalCommentFactory) events whose repeats are pure re-statements
# of an unchanged condition. Receipts, validator/focus-guard rows and anything
# else that acts as evidence keep their own origin_event_id idempotency only.
DEDUPABLE_MECHANICAL_EVENTS = frozenset({
    'status_lane.normalized',
    'status_lane.merge_sod_hold',
    'resolver.routability_hold',
    'resolver.recurring_route_discrepancy',
    'resolver.review_starvation',
    'resolver.capability_hold',
    'external_wait.human_revalidation',
})
# link.auto_block / link.auto_unblock and receipt.* rows are per-episode
# receipts (test_blocker_restore_episode): each real block/release cycle must
# keep its own row even when the rendered body is identical.

TRUNCATION_NOTE = '\n\n…[truncated {dropped} characters by Pulse comment admission; attach full logs as a file]'


class CommentRateLimited(Exception):
    """Raised by ``admit_user_comment`` when a rate limit is exceeded."""

    def __init__(self, detail, retry_after=3600):
        super().__init__(detail)
        self.detail = detail
        self.retry_after = retry_after


class CommentTooLong(Exception):
    """Raised by ``admit_user_comment`` when the body exceeds the max length."""


def _int_env(name, default):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == '':
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning('comment_admission: ignoring non-integer %s=%r', name, raw)
        return default


def _csv_env(name, default=()):
    raw = os.environ.get(name)
    if raw is None:
        return tuple(default)
    return tuple(p.strip().lower() for p in raw.split(',') if p.strip())


def automation_authors():
    return _csv_env('PULSE_AUTOMATION_COMMENT_AUTHORS', DEFAULT_EXTRA_AUTOMATION_AUTHORS)


def is_automation_author(email):
    e = (email or '').strip().lower()
    if not e:
        return False
    return e.endswith(AUTOMATION_AUTHOR_SUFFIX) or e in automation_authors()


def _is_state_marker(content):
    text = (content or '').lstrip()
    return any(text.startswith(prefix) for prefix in STATE_MARKER_PREFIXES)


def _dedupe_eligible(comment):
    if getattr(comment, '_skip_comment_admission', False):
        return False
    if comment.parent_id:
        return False
    if _is_state_marker(comment.content):
        return False
    if comment.origin_event_id:
        return (comment.origin_event or '') in DEDUPABLE_MECHANICAL_EVENTS
    return True


def clamp_automation_content(content):
    limit = _int_env('PULSE_AUTOMATION_COMMENT_MAX_CHARS', 16000)
    text = content or ''
    if limit <= 0 or len(text) <= limit:
        return text
    note = TRUNCATION_NOTE.format(dropped=len(text) - limit)
    return text[:limit] + note


def find_recent_automation_duplicate(comment):
    """Return an existing identical automation comment within the window, else None."""
    window = _int_env('PULSE_AUTOMATION_COMMENT_DEDUPE_MINUTES', 240)
    if window <= 0 or not comment.item_id:
        return None
    from ..models import Comment

    cutoff = timezone.now() - timedelta(minutes=window)
    return (
        Comment.objects.filter(
            item_id=comment.item_id,
            created_at__gte=cutoff,
            author_email=comment.author_email,
            content=comment.content,
            parent__isnull=True,
        )
        .order_by('-created_at')
        .first()
    )


def automation_admission_lock_keys(comment):
    """PLS-1478: stable two-int32 advisory-lock key for (item, author, body).

    Two concurrent sweep workers that admit the same body must map to the SAME
    lock key so the second serializes behind the first and its re-check sees
    the first worker's committed row. Different bodies/items/authors map to
    different keys and never contend.
    """
    digest = hashlib.sha256(
        f'{comment.item_id}|{comment.author_email}|{comment.content}'.encode('utf-8')
    ).digest()
    return (
        int.from_bytes(digest[0:4], 'big') & 0x7FFFFFFF,
        int.from_bytes(digest[4:8], 'big') & 0x7FFFFFFF,
    )


def acquire_automation_admission_lock(comment):
    """Take the transaction-scoped advisory lock for this comment's identity.

    MUST be called inside ``transaction.atomic()``: ``pg_advisory_xact_lock``
    is held until the surrounding transaction commits or rolls back, which is
    exactly the check-then-insert critical section. No-op on non-PostgreSQL
    vendors (house pattern, tasks.tasks.py) so the sqlite test suite is
    unaffected; sqlite serializes writers anyway.
    """
    from django.db import connection

    if connection.vendor != 'postgresql':
        return
    key_a, key_b = automation_admission_lock_keys(comment)
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [key_a, key_b])


def automation_dedupe_serialization_eligible(comment):
    """True when this new row takes the serialized (lock + re-check) save path.

    Mirrors the eligibility ``admit_automation_comment`` applies, minus the
    duplicate lookup itself — the caller uses it to decide whether the insert
    must happen under the advisory lock.
    """
    try:
        if not is_automation_author(comment.author_email):
            return False
        if not _dedupe_eligible(comment):
            return False
        window = _int_env('PULSE_AUTOMATION_COMMENT_DEDUPE_MINUTES', 240)
        return window > 0 and bool(comment.item_id)
    except Exception:  # pragma: no cover - defensive; never block a sweep
        logger.exception('comment_admission: eligibility check failed open')
        return False


def admit_automation_comment(comment):
    """Apply automation guardrails to an unsaved comment.

    Returns an existing identical ``Comment`` when the new row must not be
    inserted, otherwise ``None`` (the caller inserts ``comment``). Never raises:
    an admission failure must not break the sweep that wrote the comment.
    """
    try:
        if not is_automation_author(comment.author_email):
            return None
        # Truncation only applies to free-text legacy rows; typed mechanical
        # bodies are re-rendered from their payload and must stay exact.
        if not comment.origin_event_id:
            comment.content = clamp_automation_content(comment.content)
        if not _dedupe_eligible(comment):
            return None
        duplicate = find_recent_automation_duplicate(comment)
        if duplicate is not None:
            logger.info(
                'comment_admission: suppressed duplicate automation comment '
                'author=%s item=%s existing=%s',
                comment.author_email, comment.item_id, duplicate.pk,
            )
        return duplicate
    except Exception:  # pragma: no cover - defensive; never block a sweep
        logger.exception('comment_admission: automation admission failed open')
        return None


def _rate_exempt(email):
    e = (email or '').strip().lower()
    if not e:
        return False
    if e in _csv_env('PULSE_COMMENT_RATE_EXEMPT_EMAILS', ()):
        return True
    try:
        from .workflow_service import WorkflowService

        return bool(WorkflowService._is_operator_email(e))
    except Exception:
        return False


def admit_user_comment(*, item, author_id, author_email, content, parent=None):
    """Guardrails for the public comment create API.

    Returns an existing ``Comment`` to replay (duplicate within the window) or
    ``None`` to proceed. Raises ``CommentTooLong`` or ``CommentRateLimited``.
    """
    from ..models import Comment

    text = content or ''
    max_chars = _int_env('PULSE_COMMENT_MAX_CHARS', 65536)
    if max_chars > 0 and len(text) > max_chars:
        raise CommentTooLong(
            f'Comment is {len(text)} characters; the maximum is {max_chars}. '
            'Summarize it and attach full logs or command output as a file '
            '(POST /attachments/) instead of pasting them into a comment.'
        )

    now = timezone.now()
    dup_window = _int_env('PULSE_COMMENT_DUPLICATE_WINDOW_SECONDS', 600)
    if dup_window > 0:
        duplicate = (
            Comment.objects.filter(
                item_id=item.pk,
                created_at__gte=now - timedelta(seconds=dup_window),
                author_id=author_id,
                content=text,
                parent_id=getattr(parent, 'pk', parent),
            )
            .order_by('-created_at')
            .first()
        )
        if duplicate is not None:
            logger.info(
                'comment_admission: replayed duplicate user comment author=%s item=%s existing=%s',
                author_email, item.pk, duplicate.pk,
            )
            return duplicate

    if _rate_exempt(author_email):
        return None

    hour_ago = now - timedelta(hours=1)
    item_limit = _int_env('PULSE_COMMENT_ITEM_HOURLY_LIMIT', 30)
    if item_limit > 0:
        on_item = Comment.objects.filter(
            item_id=item.pk, created_at__gte=hour_ago, author_id=author_id,
        ).count()
        if on_item >= item_limit:
            raise CommentRateLimited(
                f'Comment rate limit: {on_item} comments on this item in the last hour '
                f'(limit {item_limit}). Consolidate updates into one comment, or update '
                'the task description/status instead of posting progress pings.'
            )
    author_limit = _int_env('PULSE_COMMENT_AUTHOR_HOURLY_LIMIT', 300)
    if author_limit > 0:
        overall = Comment.objects.filter(
            author_id=author_id, created_at__gte=hour_ago,
        ).count()
        if overall >= author_limit:
            raise CommentRateLimited(
                f'Comment rate limit: {overall} comments in the last hour across all items '
                f'(limit {author_limit}). This usually means a polling or nudge loop; '
                'stop and post one consolidated update.'
            )
    return None
