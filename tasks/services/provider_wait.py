"""PLAT-10083 durable provider_wait queue-lane classification.

The marker is proof-derived, not a new workflow status: the runtime writer
stamps it at terminal-stop time keyed on the landed
``[stop_signature: <class>]`` bounded label set, and only the two provider
classes (``upstream_unavailable``, ``quota_exhausted``) map to cluster-manager
``provider_wait`` visibility. Reversibility is explicit in both directions:

- (a) auto-clear on the seat's next SUCCESSFUL heartbeat/turn — a failed
  attempt never clears (the caller contract is success-only; Pulse-side we
  additionally refuse auto-clear while the item's latest ANDON is fresher
  than the marker's last set);
- (b) explicit manager-action clear (take_over / redispatch / acknowledge).

This is visibility, not actuation: jun's hive-actuator bounded recovery
contract consumes the signature label separately. The failover sweep
(PLAT-9836) treats active markers as legible waiting state — skip with a
typed reason, never silently swept around, never capacity-reclaimed.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from tasks.models import Item, ProviderWaitMarker

logger = logging.getLogger(__name__)

PROVIDER_WAIT_SIGNATURES = frozenset({
    'upstream_unavailable',
    'quota_exhausted',
})
MANAGER_CLEAR_ACTIONS = frozenset({'take_over', 'redispatch', 'acknowledge'})


class ProviderWaitError(ValueError):
    """Typed rejection for invalid signature or clear action."""


def set_provider_wait(item, *, signature_class, stop_message_id='', source='runtime'):
    """Activate (or refresh) the provider_wait marker for one item.

    Idempotent for writer retries: an active marker with the same signature
    AND the same stop message is a no-op refresh (last_set_at moves, nothing
    else changes). A set carrying a DIFFERENT stop message is a NEW terminal
    stop event and re-arms the marker: generation bumps, the stop message is
    replaced, and clear provenance is reset — the failed attempt stays live.
    A signature change re-arms too. Non-provider signature classes are
    typed-rejected — they must never produce provider_wait visibility.
    """
    signature = str(signature_class or '').strip()
    if signature not in PROVIDER_WAIT_SIGNATURES:
        raise ProviderWaitError(
            f"signature_class {signature!r} is not a provider_wait class; "
            f"allowed: {sorted(PROVIDER_WAIT_SIGNATURES)}"
        )
    with transaction.atomic():
        marker, created = ProviderWaitMarker.objects.get_or_create(
            item=item,
            defaults={
                'signature_class': signature,
                'stop_message_id': str(stop_message_id or '')[:64],
            },
        )
        if created:
            logger.info(
                'PLAT-10083 provider_wait set item=%s signature=%s source=%s',
                item.item_key or item.pk, signature, source,
            )
            return marker, True
        new_message = str(stop_message_id or '')[:64]
        if (marker.state == 'active' and marker.signature_class == signature
                and (marker.stop_message_id or '') == new_message):
            # PLAT-10083 review (revi P1-b): a refresh must persist
            # last_set_at. auto_now only writes when the field is saved and
            # listed in update_fields, so the no-op branch used to leave the
            # timestamp frozen at first activation — the failed-attempt guard
            # would then see the stop this very set covered as "fresher" and
            # refuse auto-clear forever. Refresh-on-set keeps the guard
            # semantically narrow: it trips only for stops whose set call
            # never happened. Same signature AND same stop message == writer
            # retry of the one stop, so this stays an idempotent no-op.
            marker.save(update_fields=['last_set_at'])
            return marker, False
        # Either the signature changed, the marker was inactive, or the same
        # signature arrived with a NEW stop message — a distinct terminal
        # stop event. Re-arm: bump the recurrence generation, replace the
        # stop message, and reset clear provenance (the marker stays live).
        marker.signature_class = signature
        marker.stop_message_id = new_message
        marker.state = 'active'
        marker.generation = (marker.generation or 0) + 1
        marker.auto_cleared_at = None
        marker.manager_cleared_at = None
        marker.manager_cleared_by = ''
        marker.manager_cleared_action = ''
        marker.save(update_fields=[
            'signature_class', 'stop_message_id', 'state', 'generation',
            'auto_cleared_at', 'manager_cleared_at', 'manager_cleared_by',
            'manager_cleared_action', 'last_set_at', 'updated_at',
        ])
        logger.info(
            'PLAT-10083 provider_wait re-armed item=%s signature=%s gen=%s',
            item.item_key or item.pk, signature, marker.generation,
        )
        return marker, True


def auto_clear_provider_wait(item, *, source='runtime-success'):
    """Clear the active marker on the seat's next SUCCESSFUL turn.

    Success-only by contract: the runtime writer calls this from the
    successful-turn path only. Defense-in-depth: if the item still carries a
    fresh terminal ANDON (newer than the marker's last set), the clear is
    refused — a failed attempt can never clear, even via a mis-wired caller.
    Returns ``(marker, cleared, refusal_reason)``; ``refusal_reason`` is a
    typed string on refusal (``'no_active_marker'`` /
    ``'fresh_terminal_andon'``), empty on success.
    """
    with transaction.atomic():
        try:
            marker = ProviderWaitMarker.objects.select_for_update().get(
                item=item, state='active',
            )
        except ProviderWaitMarker.DoesNotExist:
            return None, False, 'no_active_marker'
        if _has_fresher_terminal_andon(item, marker):
            logger.warning(
                'PLAT-10083 auto-clear refused item=%s: terminal ANDON '
                'fresher than marker last_set_at (failed-attempt guard)',
                item.item_key or item.pk,
            )
            return marker, False, 'fresh_terminal_andon'
        marker.state = 'inactive'
        marker.auto_cleared_at = timezone.now()
        marker.save(update_fields=['state', 'auto_cleared_at', 'updated_at'])
        logger.info(
            'PLAT-10083 provider_wait auto-cleared item=%s source=%s',
            item.item_key or item.pk, source,
        )
        return marker, True, ''


def manager_clear_provider_wait(item, *, action, actor):
    """Explicit manager-action clear (take_over / redispatch / acknowledge)."""
    action = str(action or '').strip()
    if action not in MANAGER_CLEAR_ACTIONS:
        raise ProviderWaitError(
            f"clear action {action!r} not allowed; allowed: {sorted(MANAGER_CLEAR_ACTIONS)}"
        )
    with transaction.atomic():
        try:
            marker = ProviderWaitMarker.objects.select_for_update().get(
                item=item, state='active',
            )
        except ProviderWaitMarker.DoesNotExist:
            return None, False
        marker.state = 'inactive'
        marker.manager_cleared_at = timezone.now()
        marker.manager_cleared_by = str(actor or '')[:254]
        marker.manager_cleared_action = action
        marker.save(update_fields=[
            'state', 'manager_cleared_at', 'manager_cleared_by',
            'manager_cleared_action', 'updated_at',
        ])
        logger.info(
            'PLAT-10083 provider_wait manager-cleared item=%s action=%s by=%s',
            item.item_key or item.pk, action, marker.manager_cleared_by,
        )
        return marker, True


def _has_fresher_terminal_andon(item, marker):
    """True when a terminal ANDON comment landed after the marker's last set.

    Terminal ANDONs carry the ``[stop_signature: <class>]`` stamp; any one
    newer than the marker means the seat re-stopped after the last successful
    set — i.e. the would-be auto-clear is riding a failed attempt.
    """
    from tasks.models import Comment
    # PLAT-10083 review (revi P1-a): the Comment text field is `content`;
    # `body` belongs to a different model and raised FieldError on every
    # guard evaluation (i.e. every auto-clear while a marker was active).
    return Comment.objects.filter(
        item=item,
        created_at__gt=marker.last_set_at,
        content__icontains='[stop_signature:',
    ).exists()


def provider_wait_visible_qs():
    """Cluster-manager visibility: items with an ACTIVE provider_wait marker.

    The manager lane reads this to see which seats/lanes are provider-waiting;
    the PLAT-9836 failover sweep uses the same set for legibility (skip with
    typed reason + touch sweep_seen_at), never silent re-routing.
    """
    return Item.objects.filter(
        provider_wait_marker__state='active',
    ).select_related('provider_wait_marker')


def mark_sweep_seen(items):
    """PLAT-9836 legibility: record that the sweep observed these markers."""
    ids = [i.id for i in items]
    if not ids:
        return 0
    return ProviderWaitMarker.objects.filter(
        item_id__in=ids, state='active',
    ).update(sweep_seen_at=timezone.now())
