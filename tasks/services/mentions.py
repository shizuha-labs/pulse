"""PLS-100: @-mention notifications on Pulse comments → instant agent DM via Connect.

When a comment is created (UI or MCP/API, both via CommentViewSet.perform_create),
parse `@handle` tokens, resolve each to a user via shizuha-id, and DM each distinct
mentioned user through Connect's internal system-message channel (the same transport
the assignment/alert DMs use). Best-effort: never raises — the comment is already saved.

PLS-735: the relay instruction must name a tool that can return comments
(``pulse_list_comments``), carry the comment id, and warn that lag × cadence can
push the target below the default newest page.
"""
import logging
import re

import requests
from django.conf import settings
from django.utils import timezone

from ..user_service import user_service
from .connect_origin import system_message_headers

logger = logging.getLogger(__name__)

# @handle charset matches shizuha-id usernames (letters, digits, dot, underscore, hyphen).
# Negative lookbehind: the @ must NOT be preceded by a handle char, so email addresses
# (`a@b.com`) and mid-word @ are not treated as mentions.
_MENTION_RE = re.compile(r'(?<![A-Za-z0-9._-])@([A-Za-z0-9._-]+)')


def extract_mentions(text):
    """Distinct @handles in `text`, lowercased, order-preserved. Pure (no I/O).

    Trailing dots are treated as sentence punctuation, not part of the handle
    (`@hana.` → `hana`). Empty results are dropped.
    """
    if not text:
        return []
    seen = set()
    out = []
    for raw in _MENTION_RE.findall(text):
        handle = raw.strip('.').lower()
        if handle and handle not in seen:
            seen.add(handle)
            out.append(handle)
    return out


def _format_posted_at(value):
    """ISO-8601 UTC timestamp for the relay body, or empty string."""
    if value is None:
        return ''
    try:
        if timezone.is_naive(value):
            value = timezone.make_aware(value, timezone.utc)
        return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        return str(value)


def _mention_message(
    author_name,
    item_key,
    title,
    excerpt,
    *,
    comment_id=None,
    posted_at=None,
):
    """Build the Connect DM body for a Pulse @-mention (PLS-100 / PLS-735).

    AC-4a: instruct ``pulse_list_comments`` (``pulse_get_task`` returns no comments).
    AC-3: carry comment id when known.
    AC-4b: note that lag × cadence can push the target below the newest page.
    """
    lines = [
        f'[Pulse mention] {author_name} mentioned you in {item_key}: "{title}"',
        f'"{excerpt}"',
    ]
    if comment_id is not None and str(comment_id) != '':
        lines.append(f'comment_id: {comment_id}')
    posted = _format_posted_at(posted_at)
    if posted:
        lines.append(f'posted: {posted}')
        lines.append(
            'On a fast thread the comment may be several pages deep in '
            'pulse_list_comments (newest first) — page deeper if the excerpt '
            'is not on the first page.'
        )
    lines.append(
        f'Use pulse_list_comments("{item_key}") for the thread '
        f'(pulse_get_task returns no comments).'
    )
    return '\n'.join(lines)


def notify_comment_mentions(item, comment, author_id, author_name):
    """Resolve @mentions in `comment` and DM each distinct mentioned user via Connect.

    Fires once per distinct user; never self-notifies the author; unknown handles are
    skipped silently. Swallows all errors (the comment save must not be affected).
    """
    try:
        handles = extract_mentions(getattr(comment, 'content', '') or '')
        if not handles:
            return
        item_key = getattr(item, 'item_key', None) or f'#{getattr(item, "id", "")}'
        title = getattr(item, 'title', '') or 'Untitled'
        excerpt = (getattr(comment, 'content', '') or '')[:200]
        comment_id = getattr(comment, 'id', None) or getattr(comment, 'pk', None)
        posted_at = getattr(comment, 'created_at', None) or getattr(comment, 'created', None)
        connect_base_url = getattr(
            settings, 'SHIZUHA_CONNECT_API_URL', 'http://shizuha-connect:8019/api'
        ).rstrip('/')

        notified = set()
        if author_id is not None:
            notified.add(author_id)  # never self-notify

        body = _mention_message(
            author_name,
            item_key,
            title,
            excerpt,
            comment_id=comment_id,
            posted_at=posted_at,
        )

        for handle in handles:
            try:
                user = user_service.get_user_by_username(handle)
            except Exception as e:
                logger.warning(f'PLS-100: resolve @{handle} failed: {e}')
                continue
            if not user:
                continue  # unknown handle — skip silently (comment still saved)
            uid = user.get('id')
            if uid is None or uid in notified:
                continue  # dedup + skip author
            notified.add(uid)
            try:
                _sm_body = {
                    'recipient_user_id': uid,
                    'content': body,
                    'sender_name': 'Pulse',
                    'sender_service': 'shizuha-pulse',
                }
                resp = requests.post(
                    f'{connect_base_url}/internal/system-message/',
                    json=_sm_body,
                    headers=system_message_headers(_sm_body),
                    timeout=5,
                )
                resp.raise_for_status()
                logger.info(f'PLS-100: mention DM sent to user {uid} (@{handle}) for {item_key}')
            except Exception as e:
                logger.warning(f'PLS-100: mention DM to user {uid} (@{handle}) failed: {e}')
    except Exception as e:
        logger.error(f'PLS-100: notify_comment_mentions error on item {getattr(item, "id", "?")}: {e}')
