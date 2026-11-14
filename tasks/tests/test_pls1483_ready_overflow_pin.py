"""PLS-1483 (PLS-509 return-side pin safety): the ready-overflow returner in
`_reclaim_wip_overload` must never shed a live PLS-233 decision pin.

Live failure (14 Nov): deliberate direct assignments made WITH a reason were
undone within minutes by the ready loop (`clear_assignment_to_group_queue(
..., 'wip_ready_overflow')`), which had no `_is_decision_pinned()` check —
HIVE-3008 → san returned 5 minutes after assignment, PLAT-11436 twice,
and PLS-1483 itself at 10:22 IST, each stamped "Returned to the **None**
team queue … assigned-ready cap is 5 (PLS-509)".

Fix contract asserted here:
  (a) a pinned ready item beyond the cap stays; an unpinned one is shed,
  (b) pinned items alone above the cap: nothing is shed + warning metric,
  (c) a stale pin (> PULSE_PIN_STALE_HOURS, default 72h, no owner activity)
      sheds like ordinary work,
  (d) a teamless shed routes to a DERIVED team, never the unwatched
      "None" queue,
  (e) the active loop keeps honoring direct_assignment (PLS-1433) unchanged.

The pull side is PLAT-9139/PLS-1433 territory (kai's rebalance sweep); this
file pins the RETURN side.
"""
import os
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import _reclaim_wip_overload


WORKER = 'worker@shizuha.com'


def _set_roster(ready_cap=5, active_cap=5):
    TeamRoster.objects.update_or_create(
        organization_id=1,
        team_name='engineering',
        defaults={
            'organization_slug': 'shizuha',
            'tiers': [[WORKER]],
            'ready_wip_cap': ready_cap,
            'active_wip_cap': active_cap,
            'is_active': True,
            'wip_enforced': True,
        },
    )


def _pinned_item(project, *, title, at=None):
    return Item.objects.create(
        title=title,
        mode='task',
        status='open',
        priority='high',
        assignment_group='engineering',
        workflow_name='autonomous-dev',
        project=project,
        assignee_email=WORKER,
        metadata={'direct_assignment': {
            'assignee_email': WORKER,
            'reason': 'deliberate lead routing',
            'pinned_team': 'engineering',
            'at': (at or timezone.now()).isoformat(),
        }},
    )


def _plain_item(project, *, title, priority='normal'):
    return Item.objects.create(
        title=title,
        mode='task',
        status='open',
        priority=priority,
        assignment_group='engineering',
        workflow_name='autonomous-dev',
        project=project,
        assignee_email=WORKER,
    )


def _unassign(item):
    item.assignee_email = None
    item.save(update_fields=['assignee_email'])


def _backdate(item, hours_ago):
    """Deterministic FIFO ordering (Item.created_at is auto_now_add)."""
    stamp = timezone.now() - timedelta(hours=hours_ago)
    Item.objects.filter(pk=item.pk).update(created_at=stamp, updated_at=stamp)
    item.refresh_from_db()


# Live-seat stand-in: the pin's seat is live right now (live evidence leg).
LIVE_SEAT = patch.object(
    WorkflowService, '_is_agent_available_for_assignment',
    return_value=True,
)


class ReadyOverflowPinExemptionTests(TestCase):
    """The five card tests, DB-backed."""

    def setUp(self):
        self.project = Project.objects.create(
            org_id=1,
            name='PLS-1483 Ready Pin Safety',
            key='PLSP',
        )

    def test_pinned_ready_item_survives_and_unpinned_overflows(self):
        # (a) ready cap 5, 6 ready items, one pinned: pinned stays, unpinned shed.
        _set_roster(ready_cap=5, active_cap=5)
        pinned = _pinned_item(self.project, title='pinned deliberate assign')
        plains = [
            _plain_item(self.project, title=f'plain {i}') for i in range(5)
        ]
        with LIVE_SEAT:
            result = _reclaim_wip_overload()
        pinned.refresh_from_db()
        self.assertEqual(
            pinned.assignee_email, WORKER,
            'a live decision pin must never be unpinned by reclaim',
        )
        shed = []
        kept = []
        for it in plains:
            it.refresh_from_db()
            (shed if not it.assignee_email else kept).append(it)
        self.assertEqual(len(shed), 1, 'exactly one unpinned ready item sheds')
        self.assertEqual(len(kept), 4)
        self.assertEqual(result['ready_queued'], 1)

    def test_pins_alone_over_cap_shed_unpinned_and_warn(self):
        # (b) 6 pins + 1 unpinned sibling, cap 5: every pin stays, the
        # unpinned sibling sheds, and the over-cap warning metric records it.
        _set_roster(ready_cap=5, active_cap=5)
        pins = [
            _pinned_item(self.project, title=f'pin {i}') for i in range(6)
        ]
        sibling = Item.objects.create(
            title='unpinned sibling',
            mode='task',
            status='open',
            priority='normal',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
            project=self.project,
            assignee_email=WORKER,
        )
        cache.delete('pulse:wip_reclaim:pinned_over_cap:last')
        with LIVE_SEAT:
            result = _reclaim_wip_overload()
        for p in pins:
            p.refresh_from_db()
            self.assertEqual(
                p.assignee_email, WORKER,
                'live pins are never unpinned even over cap',
            )
        sibling.refresh_from_db()
        self.assertIsNone(sibling.assignee_email,
                          'the unpinned sibling overflows (pins fill the quota)')
        self.assertEqual(result['ready_queued'], 1)
        payload = cache.get('pulse:wip_reclaim:pinned_over_cap:last')
        self.assertIsNotNone(payload, 'over-cap warning metric must be emitted')
        self.assertEqual(payload['state'], 'pinned_over_ready_cap')
        self.assertEqual(payload['pinned'], 6)
        self.assertEqual(payload['ready_cap'], 5)

    def test_stale_pin_sheds_like_ordinary_work(self):
        # (c) pin older than 72h with no owner activity: no longer protected.
        _set_roster(ready_cap=3, active_cap=3)
        old = _plain_item(self.project, title='old plain', priority='high')
        mid = _plain_item(self.project, title='mid plain', priority='high')
        _backdate(old, 48)
        _backdate(mid, 24)
        stale = _pinned_item(
            self.project, title='stale pin',
            at=timezone.now() - timedelta(hours=73),
        )
        fresh = _pinned_item(self.project, title='fresh pin')
        with LIVE_SEAT:
            result = _reclaim_wip_overload()
        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(fresh.assignee_email, WORKER)
        self.assertIsNone(stale.assignee_email,
                          'a stale pin (>72h, no activity) must shed')
        self.assertEqual(result['ready_queued'], 1)

    def test_teamless_shed_derives_owner_team_not_none(self):
        # (d) shed item with no assignment_group lands on the DERIVED lane.
        # The project's authoritative owner hint derives `security`, which is
        # a registered (staffed) Team — a watched lane, not "None".
        self.project.settings = {'default_implementer_group': 'security'}
        self.project.save(update_fields=['settings'])
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='security',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['sec-oncall@shizuha.com']],
                'is_active': True,
            },
        )
        _set_roster(ready_cap=1, active_cap=1)
        plain = _plain_item(self.project, title='teamless kept')
        _backdate(plain, 1)
        plain.assignment_group = None
        plain.save(update_fields=['assignment_group'])
        overflow = _plain_item(self.project, title='teamless overflow')
        overflow.assignment_group = None
        overflow.save(update_fields=['assignment_group'])
        with LIVE_SEAT:
            result = _reclaim_wip_overload()
        overflow.refresh_from_db()
        self.assertIsNone(overflow.assignee_email, 'overflow item sheds')
        self.assertEqual(
            overflow.assignment_group, 'security',
            'teamless shed must route to the derived team, never "None"',
        )
        self.assertEqual(result['ready_queued'], 1)

    def test_active_loop_direct_assignment_unchanged(self):
        # (e) the ACTIVE loop keeps its direct_assignment exemption (PLS-1433).
        _set_roster(ready_cap=2, active_cap=1)
        active = _pinned_item(self.project, title='live direct assign')
        active.status = 'in_progress'
        active.save(update_fields=['status'])
        with LIVE_SEAT:
            result = _reclaim_wip_overload()
        active.refresh_from_db()
        self.assertEqual(
            active.assignee_email, WORKER,
            'active-loop direct_assignment must stay honored',
        )
        self.assertEqual(result['active_stopped'], 0)

    def test_hibernated_enabled_pin_stays_still_protected(self):
        # (f) an ENABLED seat parked in hibernation waiting for its model
        # lease is ownership, not seat death — the pin must survive. The
        # seats sit in this state routinely (the lease-wait band), so
        # hibernation must never count as "seat gone".
        _set_roster(ready_cap=2, active_cap=2)
        parked = _pinned_item(self.project, title='parked on lease wait')
        with LIVE_SEAT, patch.object(
                WorkflowService, '_disabled_agent_emails',
                return_value=set()):
            result = _reclaim_wip_overload()
        parked.refresh_from_db()
        self.assertEqual(
            parked.assignee_email, WORKER,
            'a hibernated-but-enabled seat keeps its pin (ownership, not death)',
        )
        self.assertEqual(result['ready_queued'], 0)

    def test_disabled_seat_sheds(self):
        # (f-negative) the disabled set IS seat death: pinned items whose
        # seat is operator-killed or service-locked-out lose pin protection.
        # Two pins on one seat (cap 1): alive, both are protected (protected
        # > cap only warns); disabled, both unpin and unpin-count drops —
        # the over-cap re-shed strips them.
        _set_roster(ready_cap=1, active_cap=1)
        pinned_a = _pinned_item(self.project, title='seat disabled a')
        pinned_b = _pinned_item(self.project, title='seat disabled b')
        with patch.object(
                WorkflowService, '_disabled_agent_emails',
                return_value={WORKER}):
            result = _reclaim_wip_overload()
        pinned_a.refresh_from_db()
        pinned_b.refresh_from_db()
        shed = [p for p in (pinned_a, pinned_b) if p.assignee_email is None]
        self.assertGreaterEqual(len(shed), 1,
                                'a disabled seat loses pin protection')
        self.assertEqual(result['ready_queued'], 1)
