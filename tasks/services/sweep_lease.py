"""PLAT-11947 CAS lease for sweep verdict posts.

A worker may post a sweep verdict only while it holds the current generation
for that target. A superseded writer gets a typed rejection and must move on.
A lease that outlives its TTL expires loudly on the next touch so a crashed
worker cannot pin the slot.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from tasks.models import Item, SweepLeaseExpired, StaleSweepWriter, SweepWorkLease

LEASE_TTL = timedelta(minutes=5)


class SweepLeaseLost(Exception):
    """The holder lost the lease between acquire and verdict post."""

    def __init__(self, item_key, reason):
        self.item_key = item_key
        self.reason = reason
        super().__init__(f'sweep lease lost for {item_key}: {reason}')

    def __reduce__(self):
        # Django's parallel runner pickles failures. The default reduce
        # calls Exception.__init__ with the message only.
        return (self.__class__, (self.item_key, self.reason))


def _lock_pair(item):
    locked = Item.objects.select_for_update().get(pk=item.pk)
    lease = SweepWorkLease.objects.select_for_update().filter(item=locked).first()
    return locked, lease


def _commit_loud_expiry(item, now):
    """Commit a lapsed lease's loud-expiry mark before the caller raises.

    Must run outside ``transaction.atomic()``. The acquire/post paths raise
    after the mark, and a save inside that block rolls back with the
    exception (PLAT-11947 premerge: ``expired_loudly`` stayed False).
    """
    SweepWorkLease.objects.filter(
        item=item, expires_at__lte=now, expired_loudly=False,
    ).update(expired_loudly=True, updated_at=timezone.now())


def acquire_sweep_lease(item, holder, generation, now=None):
    """CAS the lease to ``generation``.

    A strictly newer held generation rejects the caller as a stale writer.
    An equal generation refreshes only for the current holder. A lapsed lease
    is marked expired exactly once, and that first touch raises so the lapse
    is loud. The next acquire reopens the slot for a new holder: a crashed
    worker must not keep the fence closed after its TTL (PLAT-11947 review).
    """
    now = now or timezone.now()
    generation = int(generation)
    holder = str(holder or '')
    # Lock first. A pre-lock mark misses a lease the test (or a crash) backdates
    # after the previous acquire, and the in-lock path then rewrites it as live.
    with transaction.atomic():
        locked, lease = _lock_pair(item)
        lapsed_unmarked = bool(
            lease and lease.expires_at <= now and not lease.expired_loudly
        )
    if lapsed_unmarked:
        # Own commit, outside the atomic that raises, or the mark rolls back.
        _commit_loud_expiry(item, now)
        raise SweepLeaseExpired(item.item_key, lease.expires_at)
    with transaction.atomic():
        locked, lease = _lock_pair(item)
        if lease and lease.expires_at <= now and lease.expired_loudly:
            # Already loud once. Reopen for the new holder.
            pass
        elif lease and lease.expires_at <= now:
            raise SweepLeaseExpired(locked.item_key, lease.expires_at)
        live = bool(lease and lease.expires_at > now)
        if live and lease.generation > generation:
            raise StaleSweepWriter(locked.item_key, lease.generation, generation)
        if live and lease.generation == generation and lease.holder != holder:
            raise StaleSweepWriter(locked.item_key, lease.generation, generation)
        if lease is None:
            return SweepWorkLease.objects.create(
                item=locked, generation=generation, holder=holder,
                expires_at=now + LEASE_TTL,
            )
        lease.generation = generation
        lease.holder = holder
        lease.expires_at = now + LEASE_TTL
        lease.expired_loudly = False
        lease.save(update_fields=[
            'generation', 'holder', 'expires_at', 'expired_loudly', 'updated_at',
        ])
        return lease


def post_sweep_verdict(item, holder, generation, post, now=None):
    """Run ``post`` only if this worker still holds ``generation``.

    ``post`` is called inside the row lock, after the CAS re-check, and must
    not itself commit a conflicting verdict. A stale or lapsed holder is
    rejected and ``post`` is not called. The caller logs the loss and continues
    the sweep; this function does not retry.
    """
    now = now or timezone.now()
    generation = int(generation)
    holder = str(holder or '')
    _commit_loud_expiry(item, now)
    with transaction.atomic():
        locked, lease = _lock_pair(item)
        if (
            lease is None
            or lease.holder != holder
            or lease.generation != generation
            or lease.expires_at <= now
        ):
            reason = 'missing'
            if lease is not None and lease.expires_at <= now:
                # Same typed expiry as acquire. SweepLeaseLost is not
                # pickle-safe for the parallel runner and is the wrong type
                # for a lapse the caller already marked loudly.
                raise SweepLeaseExpired(locked.item_key, lease.expires_at)
            elif lease is not None and (
                lease.holder != holder or lease.generation != generation
            ):
                raise StaleSweepWriter(
                    locked.item_key, lease.generation, generation,
                )
            raise SweepLeaseLost(getattr(locked, 'item_key', None) or locked.pk, reason)
        return post(locked)
