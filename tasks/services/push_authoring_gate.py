"""PLAT-11817: refuse code-fix authoring ownership for a seat that cannot push.

The capability registry lives on the item metadata under
``seat_capabilities``: ``{seat_email: {repo: {"push": bool, "probed_at": iso}}}``.
A missing or expired entry is no-push (fail closed). This module never probes
Origin and never mints a credential; recording a fresh probe result is the
operator/devops lane's job. The gate only reads what was recorded.

The handoff half verifies a sha256 pin before a blob may be applied. A
mismatch raises and records nothing that would invite a silent retry.
"""

import hashlib
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

CAPABILITY_TTL = timedelta(hours=24)


class PushAuthoringRefused(Exception):
    """Assignee cannot author a code fix because push capability is absent."""

    def __init__(self, message, *, code):
        super().__init__(message)
        self.code = code


class HandoffRejected(Exception):
    """A patch handoff failed its integrity or currency check."""

    def __init__(self, message, *, code):
        super().__init__(message)
        self.code = code


def _seat_key(email):
    return str(email or '').strip().lower()


def code_fix_repo(item):
    """Repo a code fix would land in, or '' when the item is not a code fix."""
    meta = getattr(item, 'metadata', None) or {}
    repo = str(meta.get('origin_repo') or getattr(item, 'origin_repo', '') or '').strip()
    labels = {str(label).strip().lower() for label in (getattr(item, 'labels', None) or [])}
    if repo:
        return repo
    if 'code-fix' in labels:
        return str(meta.get('target_repo') or '').strip()
    return ''


def push_capable(item, seat_email, repo, *, now=None):
    """True only for a fresh recorded ``push: true``. Unknown or stale is False."""
    meta = getattr(item, 'metadata', None) or {}
    registry = meta.get('seat_capabilities') or {}
    entry = (registry.get(_seat_key(seat_email)) or {}).get(repo) or {}
    if entry.get('push') is not True:
        return False
    probed = parse_datetime(str(entry.get('probed_at') or ''))
    if probed is None:
        return False
    if timezone.is_naive(probed):
        probed = timezone.make_aware(probed, timezone.get_current_timezone())
    current = now or timezone.now()
    return current - probed <= CAPABILITY_TTL


def assert_can_author(item, seat_email, *, now=None):
    """Refuse authoring ownership for a no-push seat. No-op for non-code items."""
    repo = code_fix_repo(item)
    if not repo:
        return
    if push_capable(item, seat_email, repo, now=now):
        return
    seat = _seat_key(seat_email) or '<unassigned>'
    raise PushAuthoringRefused(
        f'assignee {seat} cannot push to {repo} '
        '(credential lacks write:repository)',
        code='no_push_capability',
    )


def verify_handoff(blob, declared_sha256, *, current_base_sha, blob_base_sha):
    """Reject a handoff whose bytes or base commit do not match the pin.

    Returns the verified sha256. Raises HandoffRejected otherwise. Callers
    must not retry the same pin; a resend is a new comment with a new hash.
    """
    raw = blob if isinstance(blob, (bytes, bytearray)) else str(blob).encode()
    actual = hashlib.sha256(raw).hexdigest()
    pinned = str(declared_sha256 or '').strip().lower().removeprefix('sha256:')
    if not pinned or actual != pinned:
        raise HandoffRejected(
            'handoff sha256 mismatch; rejected without retry',
            code='sha256_mismatch',
        )
    if not blob_base_sha or blob_base_sha != current_base_sha:
        raise HandoffRejected(
            'handoff base commit is no longer current; re-derive required',
            code='base_not_current',
        )
    return actual
