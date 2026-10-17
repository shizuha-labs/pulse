"""HIVE-608: Pulse source producer for HomeActivityEventV1 (HLD 1f271d2c).

Emits display-safe activity events to per-org Redis Streams
``home:activity:v1:org:{org_id}`` on the Pulse write paths (comment created,
status transition, assignment changed, issue/PR link created). Events are
produced from the existing write paths — never a polling sweep — and published
transaction-after-commit so the deep-link target row exists before Home shows it.

Security (HLD §8, STRIDE §9):
- ``org_id`` is MANDATORY and comes from the item's project organization. Events
  whose org cannot be proven are DROPPED, never published (fail closed).
- ``summary`` / ``title`` are capped, whitespace-collapsed display teasers — no
  secrets, raw tokens, or long comment bodies leak (truncation is the control).
- Best effort: a Redis outage or any producer error never breaks the originating
  write path (all publish failures are swallowed + logged by id/type/org only).
- Per-org key isolation: the stream key embeds the org id; no global stream.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from django.conf import settings
from django.db import transaction

from ..secret_scanner import has_secrets, redact_secrets

logger = logging.getLogger(__name__)

STREAM_VERSION = 1
STREAM_KEY_FMT = 'home:activity:v1:org:{org_id}'
STREAM_MAXLEN = 10_000            # bounded retention per HLD §4 (~10k events/org)
STREAM_24H_MS = 86_400_000        # 24 hours in milliseconds for MINID trim
SUMMARY_MAX = 180                 # display-safe teaser cap
TITLE_MAX = 160
META_STR_MAX = 120                # per-value metadata cap (defensive, no leakage)

VALID_TYPES = {'task.comment', 'task.transition', 'task.assignment', 'task.pr_link'}
_VALID_PRIORITY = {'low', 'normal', 'medium', 'high', 'urgent'}
_ACTOR_KEYS = ('kind', 'username', 'display_name', 'role')

_redis_client = None  # module-level cache; overridable in tests via set_redis_client


def set_redis_client(client) -> None:
    """Test seam: inject a fake redis client (e.g. fakeredis) or None to reset."""
    global _redis_client
    _redis_client = client


def _get_redis():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis  # local import: optional at import time / hermetic tests
        _redis_client = redis.Redis.from_url(
            settings.CELERY_BROKER_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    except Exception:
        logger.exception('home_activity: redis client init failed')
        return None
    return _redis_client


def _cap(text, limit) -> str:
    """Whitespace-collapse, REDACT secrets, then truncate to a display teaser.

    HIVE-608 review (rei): truncation alone is not a redaction control — a
    secret near the START of a long body (e.g. the first 180 chars of a raw
    comment) survives the ``[:limit]`` cap. So redaction must run BEFORE the
    cap, over the full string, on every free-text field routed through here
    (summary, title, actor, metadata values). Uses the existing repo secret
    scanner so the feed never publishes raw tokens.
    """
    s = ' '.join((text or '').split())
    s, _ = redact_secrets(s)
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + '…'
    return s


def _project_org_id(item):
    """Mandatory org provenance: the item's project organization id (int > 0)."""
    project = getattr(item, 'project', None)
    org_id = getattr(project, 'org_id', None) if project is not None else None
    try:
        org_id = int(org_id)
    except (TypeError, ValueError):
        return None
    return org_id if org_id > 0 else None


def _href_for(item) -> str:
    key = getattr(item, 'item_key', None) or ''
    return f'/pulse/browse/{key}' if key else '/pulse'


def _clean_actor(actor) -> dict:
    actor = actor or {}
    kind = actor.get('kind') if isinstance(actor, dict) else None
    out = {'kind': kind if kind in ('agent', 'user', 'system') else 'system'}
    for k in _ACTOR_KEYS[1:]:
        v = actor.get(k) if isinstance(actor, dict) else None
        if v:
            out[k] = _cap(str(v), META_STR_MAX)
    return out


def _clean_metadata(metadata) -> dict:
    if not isinstance(metadata, dict):
        return {}
    out = {}
    for k, v in metadata.items():
        if v is None or isinstance(v, bool):
            out[str(k)[:64]] = v
        elif isinstance(v, (int, float)):
            out[str(k)[:64]] = v
        else:
            out[str(k)[:64]] = _cap(str(v), META_STR_MAX)
    return out


def build_event(*, event_type, item, actor, summary, priority='normal', metadata=None):
    """Build a HomeActivityEventV1 dict, or ``None`` if it must not be published.

    Pure and side-effect free so the security contract (org-mandatory / fail
    closed, summary + title capping, schema, valid type) is unit-testable
    without Redis.
    """
    if event_type not in VALID_TYPES:
        return None
    org_id = _project_org_id(item)
    if org_id is None:
        # Fail closed: an event whose tenant/org cannot be proven is dropped.
        return None
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    project = getattr(item, 'project', None)
    # Report whether the display teaser had to be redacted (the values below are
    # all scrubbed via _cap; this flag reflects that a secret was present in a
    # raw free-text source so downstream/Home can surface a "redacted" marker).
    _raw_free_text = [summary or '', getattr(item, 'title', '') or '']
    if isinstance(metadata, dict):
        # All metadata values are stringified via _cap(str(v)) → include every
        # value in redaction detection, not just str-typed ones (a list containing
        # a PAT would be stringified+redacted but missed by isinstance(v, str)).
        _raw_free_text += [str(v) for v in metadata.values()]
    # Actor fields are also scrubbed via _cap → include them in redaction detection
    # so a redacted actor doesn't report redaction='none'.
    if isinstance(actor, dict):
        _raw_free_text += [str(v) for v in actor.values()]
    redaction = 'suppressed' if any(has_secrets(t) for t in _raw_free_text) else 'none'
    return {
        'version': STREAM_VERSION,
        'source': 'pulse',
        'type': event_type,
        'occurred_at': now,
        'org_id': org_id,
        'project_key': (getattr(project, 'key', '') or '') if project is not None else '',
        'actor': _clean_actor(actor),
        'target': {
            'kind': 'task',
            'key': getattr(item, 'item_key', '') or '',
            'title': _cap(getattr(item, 'title', ''), TITLE_MAX),
            'href': _href_for(item),
        },
        'summary': _cap(summary, SUMMARY_MAX),
        'redaction': redaction,
        'priority': priority if priority in _VALID_PRIORITY else 'normal',
        'metadata': _clean_metadata(metadata),
    }


def publish(event) -> bool:
    """Best-effort XADD to the event's per-org stream. Never raises."""
    if not event or not event.get('org_id'):
        return False
    client = _get_redis()
    if client is None:
        return False
    try:
        key = STREAM_KEY_FMT.format(org_id=event['org_id'])
        client.xadd(
            key,
            {'event': json.dumps(event, ensure_ascii=False)},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        # HLD §4: retain 24h or 10k events/org, whichever is smaller.
        # XTRIM MINID evicts entries with IDs older than (now - 24h).
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        # Exact MINID (no ~) so low-volume orgs actually evict entries older than 24h.
        # Redis 7: approximate MINID can leave entries past the cutoff when the stream
        # is small; exact MINID guarantees eviction of all IDs < the given timestamp.
        client.xtrim(key, minid=str(now_ms - STREAM_24H_MS), approximate=False)
        return True
    except Exception:
        # Log by id/type/org only — never the payload (HLD §8).
        logger.warning(
            'home_activity: publish failed org=%s type=%s',
            event.get('org_id'), event.get('type'), exc_info=True,
        )
        return False


def _emit_after_commit(event) -> None:
    """Publish transaction-after-commit so the source row exists first.

    Outside an atomic block Django runs the callback immediately, which is still
    after the caller saved the row — correct for the non-transactional path.
    """
    if not event:
        return
    transaction.on_commit(lambda: publish(event))


def _looks_agent_email(email) -> bool:
    return bool(email) and '@agents.' in email.lower()


def _username(email) -> str:
    return (email or '').split('@', 1)[0]


def _actor_from_user(user):
    """Normalize a Django/auth user (or dict) into an event actor."""
    if user is None:
        return {'kind': 'system'}
    if isinstance(user, dict):
        return user
    email = getattr(user, 'email', '') or ''
    username = getattr(user, 'username', '') or _username(email)
    return {
        'kind': 'agent' if _looks_agent_email(email) else 'user',
        'username': username,
        'display_name': (getattr(user, 'get_full_name', lambda: '')() or username),
        'role': getattr(user, 'role', '') or '',
    }


# ── Public emit helpers (called from the existing write paths) ────────────────

def emit_comment(comment) -> None:
    item = getattr(comment, 'item', None)
    if item is None:
        return
    email = getattr(comment, 'author_email', '') or ''
    actor = {
        'kind': 'agent' if _looks_agent_email(email) else 'user',
        'username': _username(email),
        'display_name': getattr(comment, 'author_name', '') or _username(email),
    }
    who = actor['display_name'] or 'Someone'
    # comment.content is redacted + capped by _cap → neither a long body nor a
    # secret/token near the start of the body can leak into the Home teaser.
    summary = f'{who} commented on {getattr(item, "item_key", "")}: {getattr(comment, "content", "")}'
    _emit_after_commit(build_event(
        event_type='task.comment', item=item, actor=actor, summary=summary,
        priority=getattr(item, 'priority', 'normal'),
        metadata={'comment_id': str(getattr(comment, 'id', ''))},
    ))


def emit_transition(item, *, user=None, from_status='', to_status='') -> None:
    actor = _actor_from_user(user)
    who = actor.get('display_name') or actor.get('username') or 'Someone'
    summary = f'{who} moved {getattr(item, "item_key", "")} to {to_status}'
    _emit_after_commit(build_event(
        event_type='task.transition', item=item, actor=actor, summary=summary,
        priority=getattr(item, 'priority', 'normal'),
        metadata={'from_status': from_status, 'to_status': to_status},
    ))


def emit_assignment(item, *, user=None, assignee_email='', assignee_name='') -> None:
    actor = _actor_from_user(user)
    who = actor.get('display_name') or actor.get('username') or 'Someone'
    target_who = assignee_name or _username(assignee_email) or 'unassigned'
    summary = f'{who} assigned {getattr(item, "item_key", "")} to {target_who}'
    _emit_after_commit(build_event(
        event_type='task.assignment', item=item, actor=actor, summary=summary,
        priority=getattr(item, 'priority', 'normal'),
        metadata={'assignee': _username(assignee_email)},
    ))


def emit_pr_link(item, *, user=None, link_type='', target_key='', url='') -> None:
    actor = _actor_from_user(user)
    who = actor.get('display_name') or actor.get('username') or 'Someone'
    rel = (link_type or 'linked').replace('_', ' ')
    tail = target_key or (url and 'a pull request') or 'a related item'
    summary = f'{who} {rel} {getattr(item, "item_key", "")} ↔ {tail}'
    _emit_after_commit(build_event(
        event_type='task.pr_link', item=item, actor=actor, summary=summary,
        priority=getattr(item, 'priority', 'normal'),
        metadata={'link_type': link_type, 'target_key': target_key},
    ))
