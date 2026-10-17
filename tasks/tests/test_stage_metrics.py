"""PLS-13: stage-dwell derivation tests (pure — no DB)."""
from datetime import datetime, timedelta, timezone as tz

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from tasks.models import Item, ItemActivity
from tasks.services.stage_metrics import derive_intervals, _percentile
from tasks.signals import TRACKED_FIELDS

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=tz.utc)


class DeriveIntervalsTests(SimpleTestCase):
    def test_pairs_transitions_into_intervals(self):
        transitions = [
            {'old': 'Open', 'new': 'In Progress', 'at': T0 + timedelta(hours=2)},
            {'old': 'In Progress', 'new': 'Done', 'at': T0 + timedelta(hours=5)},
        ]
        iv = derive_intervals('Done', T0, transitions, now=T0 + timedelta(hours=6))
        self.assertEqual([x['status'] for x in iv], ['Open', 'In Progress', 'Done'])
        self.assertEqual(iv[0]['duration_seconds'], 7200)   # Open: 0–2h
        self.assertEqual(iv[1]['duration_seconds'], 10800)  # In Progress: 2–5h
        self.assertEqual(iv[2]['duration_seconds'], 3600)   # Done: 5–6h (open)
        self.assertFalse(iv[0]['open'])
        self.assertTrue(iv[2]['open'])
        self.assertIsNone(iv[2]['exited_at'])

    def test_initial_status_comes_from_first_transition_old_value(self):
        transitions = [{'old': 'Triage', 'new': 'Open', 'at': T0 + timedelta(hours=1)}]
        iv = derive_intervals('Open', T0, transitions, now=T0 + timedelta(hours=2))
        self.assertEqual(iv[0]['status'], 'Triage')   # not the current 'Open'
        self.assertEqual(iv[0]['duration_seconds'], 3600)

    def test_no_transitions_single_open_interval(self):
        iv = derive_intervals('Open', T0, [], now=T0 + timedelta(hours=1))
        self.assertEqual(len(iv), 1)
        self.assertEqual(iv[0]['status'], 'Open')
        self.assertTrue(iv[0]['open'])
        self.assertEqual(iv[0]['duration_seconds'], 3600)

    def test_percentile_nearest_rank(self):
        self.assertEqual(_percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90), 9)
        self.assertEqual(_percentile([5], 90), 5)
        self.assertIsNone(_percentile([], 90))

    def test_parking_status_helper_excludes_scheduled_and_blocked(self):
        from tasks.services.stage_metrics import is_parking_status
        self.assertTrue(is_parking_status('scheduled'))
        self.assertTrue(is_parking_status('Scheduled'))
        self.assertTrue(is_parking_status('blocked'))
        self.assertTrue(is_parking_status('backlog'))
        self.assertFalse(is_parking_status('needs_triage'))
        self.assertFalse(is_parking_status('in_progress'))
        self.assertFalse(is_parking_status('accepted'))


class TeamThroughputParkingTests(TestCase):
    """Org-progress 'slowest stages' must not rank calendar/blocked parking."""

    def test_team_throughput_omits_parking_by_default(self):
        from tasks.services.stage_metrics import team_throughput
        field = TRACKED_FIELDS.get('status', 'Status')
        now = timezone.now()

        scheduled = Item.objects.create(
            mode='task', title='annual filing', status='open',
            assignment_group='devops')
        Item.objects.filter(pk=scheduled.pk).update(created_at=now - timedelta(days=40))
        # entered scheduled 35d ago, exited 1d ago → 34d closed dwell in-window
        act = ItemActivity.objects.create(
            item=scheduled, field_name=field, old_value='scheduled',
            new_value='open', user_id=1)
        ItemActivity.objects.filter(pk=act.pk).update(created_at=now - timedelta(days=1))
        # seed entry into scheduled so the closed interval is long
        act0 = ItemActivity.objects.create(
            item=scheduled, field_name=field, old_value='open',
            new_value='scheduled', user_id=1)
        ItemActivity.objects.filter(pk=act0.pk).update(created_at=now - timedelta(days=35))

        stuck = Item.objects.create(
            mode='task', title='triage lag', status='in_progress',
            assignment_group='architecture')
        Item.objects.filter(pk=stuck.pk).update(created_at=now - timedelta(days=10))
        a1 = ItemActivity.objects.create(
            item=stuck, field_name=field, old_value='needs_triage',
            new_value='in_progress', user_id=1)
        ItemActivity.objects.filter(pk=a1.pk).update(created_at=now - timedelta(days=1))
        a0 = ItemActivity.objects.create(
            item=stuck, field_name=field, old_value='open',
            new_value='needs_triage', user_id=1)
        ItemActivity.objects.filter(pk=a0.pk).update(created_at=now - timedelta(days=8))

        default_rows = team_throughput(days=14, now=now)
        statuses = {(r['team'], r['status']) for r in default_rows}
        self.assertNotIn(('devops', 'scheduled'), statuses)
        self.assertIn(('architecture', 'needs_triage'), statuses)

        with_parking = team_throughput(days=14, now=now, include_parking=True)
        parking_statuses = {(r['team'], r['status']) for r in with_parking}
        self.assertIn(('devops', 'scheduled'), parking_statuses)
        sched = next(r for r in with_parking if r['team'] == 'devops' and r['status'] == 'scheduled')
        self.assertTrue(sched['is_parking'])


class CurrentStageDwellTests(TestCase):
    """Home 'slowest stages' must rank items sitting NOW, not closed history."""

    def test_current_stage_dwell_uses_open_tail_and_skips_parking(self):
        from tasks.services.stage_metrics import current_stage_dwell
        field = TRACKED_FIELDS.get('status', 'Status')
        now = timezone.now()

        sitting = Item.objects.create(
            mode='task', title='still in triage', status='needs_triage',
            assignment_group='review')
        Item.objects.filter(pk=sitting.pk).update(created_at=now - timedelta(days=16))
        entered = ItemActivity.objects.create(
            item=sitting, field_name=field, old_value='open',
            new_value='needs_triage', user_id=1)
        ItemActivity.objects.filter(pk=entered.pk).update(
            created_at=now - timedelta(days=16))

        already_left = Item.objects.create(
            mode='task', title='old accepted', status='in_progress',
            assignment_group='devops')
        Item.objects.filter(pk=already_left.pk).update(created_at=now - timedelta(days=30))
        a0 = ItemActivity.objects.create(
            item=already_left, field_name=field, old_value='accepted',
            new_value='in_progress', user_id=1)
        ItemActivity.objects.filter(pk=a0.pk).update(created_at=now - timedelta(days=1))
        a1 = ItemActivity.objects.create(
            item=already_left, field_name=field, old_value='open',
            new_value='accepted', user_id=1)
        ItemActivity.objects.filter(pk=a1.pk).update(created_at=now - timedelta(days=21))

        parked = Item.objects.create(
            mode='task', title='calendar hold', status='scheduled',
            assignment_group='admin-ops')
        Item.objects.filter(pk=parked.pk).update(created_at=now - timedelta(days=40))

        rows = current_stage_dwell(now=now)
        keys = {(r['team'], r['status']) for r in rows}
        self.assertIn(('review', 'needs_triage'), keys)
        self.assertNotIn(('devops', 'accepted'), keys)
        self.assertNotIn(('admin-ops', 'scheduled'), keys)
        triage = next(r for r in rows if r['team'] == 'review' and r['status'] == 'needs_triage')
        self.assertEqual(triage['samples'], 1)
        self.assertGreater(triage['avg_dwell_seconds'], 15 * 86400)
        self.assertEqual(triage['kind'], 'open')


class DeriveCycleTests(SimpleTestCase):
    """PLS-362: cycle/lead derivation (pure — no DB)."""

    def test_cycle_starts_at_first_transition_lead_at_creation(self):
        transitions = [
            {'old': 'Open', 'new': 'In Progress', 'at': T0 + timedelta(hours=2)},
            {'old': 'In Progress', 'new': 'Done', 'at': T0 + timedelta(hours=5)},
        ]
        from tasks.services.stage_metrics import derive_cycle
        cycle, lead = derive_cycle(T0, T0 + timedelta(hours=5), transitions)
        self.assertEqual(lead, 5 * 3600)
        self.assertEqual(cycle, 3 * 3600)  # 2h -> 5h

    def test_no_transitions_falls_back_to_lead(self):
        from tasks.services.stage_metrics import derive_cycle
        cycle, lead = derive_cycle(T0, T0 + timedelta(hours=4), [])
        self.assertEqual(cycle, lead)
        self.assertEqual(lead, 4 * 3600)

    def test_backdated_completion_clamps_to_zero_not_negative(self):
        transitions = [{'old': 'Open', 'new': 'In Progress', 'at': T0 + timedelta(hours=6)}]
        from tasks.services.stage_metrics import derive_cycle
        cycle, lead = derive_cycle(T0, T0 + timedelta(hours=5), transitions)
        self.assertEqual(cycle, 0.0)
        self.assertEqual(lead, 5 * 3600)


class CycleTimesAggregateTests(TestCase):
    """PLS-362: cycle_times windowing, alert exclusion, and summary (DB)."""

    def test_cycle_times_rows_window_and_summary(self):
        from tasks.services.stage_metrics import cycle_times
        field = TRACKED_FIELDS.get('status', 'Status')
        now = timezone.now()

        done_in = Item.objects.create(
            mode='task', title='in window', status='done',
            assignment_group='engineering')
        Item.objects.filter(pk=done_in.pk).update(
            created_at=now - timedelta(days=3), completed_at=now - timedelta(days=1))
        act = ItemActivity.objects.create(
            item=done_in, field_name=field, old_value='open', new_value='in_progress',
            user_id=1)
        # created_at is auto_now_add — backdate via update()
        ItemActivity.objects.filter(pk=act.pk).update(created_at=now - timedelta(days=2))

        done_old = Item.objects.create(
            mode='task', title='out of window', status='done',
            assignment_group='engineering')
        Item.objects.filter(pk=done_old.pk).update(
            created_at=now - timedelta(days=40), completed_at=now - timedelta(days=35))

        alert_done = Item.objects.create(
            mode='alert', title='alert', status='resolved',
            assignment_group='devops')
        Item.objects.filter(pk=alert_done.pk).update(
            created_at=now - timedelta(days=2), completed_at=now - timedelta(days=1))

        Item.objects.create(mode='task', title='still open', status='open',
                            assignment_group='engineering')

        payload = cycle_times(days=14, now=now)
        keys = [r['item_key'] for r in payload['rows']]

        self.assertEqual(len(payload['rows']), 1)
        self.assertIn(done_in.item_key or str(done_in.pk), keys)
        row = payload['rows'][0]
        # cycle = first transition (d-2) -> completed (d-1) = 1 day
        self.assertAlmostEqual(row['cycle_seconds'], 86400.0, delta=5)
        # lead = created (d-3) -> completed (d-1) = 2 days
        self.assertAlmostEqual(row['lead_seconds'], 2 * 86400.0, delta=5)
        self.assertEqual(payload['summary']['count'], 1)
        self.assertAlmostEqual(payload['summary']['p50_seconds'], 86400.0, delta=5)


class CumulativeFlowTests(TestCase):
    """PLS-361: end-of-day status counts for the CFD."""

    def test_daily_snapshots_track_transitions(self):
        from tasks.services.stage_metrics import cumulative_flow
        field = TRACKED_FIELDS.get('status', 'Status')
        now = timezone.now()

        item = Item.objects.create(mode='task', title='flows', status='done',
                                   assignment_group='engineering')
        Item.objects.filter(pk=item.pk).update(created_at=now - timedelta(days=3))
        for old, new, days_ago in (('open', 'in_progress', 2), ('in_progress', 'done', 1)):
            act = ItemActivity.objects.create(
                item=item, field_name=field, old_value=old, new_value=new, user_id=1)
            ItemActivity.objects.filter(pk=act.pk).update(
                created_at=now - timedelta(days=days_ago, hours=1))

        payload = cumulative_flow(days=4, now=now)

        self.assertEqual(len(payload['days']), 4)
        # Day boundaries (now-3d, now-2d, now-1d, now): open, in_progress, done, done.
        self.assertEqual(payload['series'].get('open', [0]*4)[0], 1)
        self.assertEqual(payload['series'].get('in_progress', [0]*4)[1], 1)
        self.assertEqual(payload['series'].get('done', [0]*4)[2], 1)
        self.assertEqual(payload['series'].get('done', [0]*4)[3], 1)
        # Non-terminal statuses ordered before terminal ones for stacking.
        self.assertLess(payload['statuses'].index('open'), payload['statuses'].index('done'))

    def test_alert_mode_and_prewindow_terminal_items_excluded(self):
        from tasks.services.stage_metrics import cumulative_flow
        now = timezone.now()
        Item.objects.create(mode='alert', title='alert', status='firing',
                            assignment_group='devops')
        old_done = Item.objects.create(mode='task', title='ancient', status='done',
                                       assignment_group='engineering')
        Item.objects.filter(pk=old_done.pk).update(
            created_at=now - timedelta(days=100), updated_at=now - timedelta(days=90))

        payload = cumulative_flow(days=7, now=now)
        self.assertEqual(sum(payload['series'].get('firing', [0])), 0)
        self.assertEqual(sum(payload['series'].get('done', [0])), 0)
        self.assertTrue(payload['done_baseline_excluded'])

    def test_window_bounded_at_90_days(self):
        from tasks.services.stage_metrics import cumulative_flow
        payload = cumulative_flow(days=400, now=timezone.now())
        self.assertEqual(payload['window_days'], 90)
        self.assertEqual(len(payload['days']), 90)


class ProgressTimeseriesTests(TestCase):
    """Org-progress trend: bucketed created/completed/terminal counts."""

    def test_buckets_created_and_terminal_transitions(self):
        from tasks.services.stage_metrics import progress_timeseries
        field = TRACKED_FIELDS.get('status', 'Status')
        now = timezone.now()

        # Item created 3h ago, moved to 'done' (productive terminal) 1h ago.
        done_item = Item.objects.create(mode='task', title='shipped', status='done',
                                        assignment_group='engineering')
        Item.objects.filter(pk=done_item.pk).update(created_at=now - timedelta(hours=3))
        act = ItemActivity.objects.create(
            item=done_item, field_name=field, old_value='in_progress',
            new_value='done', user_id=1)
        ItemActivity.objects.filter(pk=act.pk).update(created_at=now - timedelta(hours=1))

        # Item created 2h ago, CANCELLED 30m ago (terminal but not "completed").
        drop_item = Item.objects.create(mode='task', title='dropped', status='cancelled',
                                        assignment_group='engineering')
        Item.objects.filter(pk=drop_item.pk).update(created_at=now - timedelta(hours=2))
        act2 = ItemActivity.objects.create(
            item=drop_item, field_name=field, old_value='open',
            new_value='cancelled', user_id=1)
        ItemActivity.objects.filter(pk=act2.pk).update(created_at=now - timedelta(minutes=30))

        payload = progress_timeseries(hours=4, buckets=4, now=now)

        self.assertEqual(payload['buckets'], 4)
        self.assertEqual(len(payload['points']), 4)
        # 2 created (3h & 2h ago), 2 terminal transitions, 1 of them productive.
        self.assertEqual(payload['totals']['created'], 2)
        self.assertEqual(payload['totals']['terminal'], 2)
        self.assertEqual(payload['totals']['completed'], 1)

    def test_alert_mode_excluded_and_bounds(self):
        from tasks.services.stage_metrics import progress_timeseries
        now = timezone.now()
        Item.objects.create(mode='alert', title='noise', status='firing',
                            assignment_group='devops')
        payload = progress_timeseries(hours=99999, buckets=9999, now=now)
        # Alert-mode rows never count toward progress.
        self.assertEqual(payload['totals']['created'], 0)
        # hours capped at 30d, buckets capped at 500.
        self.assertLessEqual(payload['hours'], 24 * 30)
        self.assertLessEqual(payload['buckets'], 500)


def _make_item(**kwargs):
    """Create a task-mode item backdated to `created_at` (default 5d ago)."""
    now = kwargs.pop('now', timezone.now())
    created_at = kwargs.pop('created_at', now - timedelta(days=5))
    item = Item.objects.create(mode='task', **kwargs)
    Item.objects.filter(pk=item.pk).update(created_at=created_at)
    item.refresh_from_db()
    return item


def _transition(item, old, new, at, action='status_changed'):
    field = TRACKED_FIELDS.get('status', 'Status')
    act = ItemActivity.objects.create(
        item=item, action=action, field_name=field,
        old_value=old, new_value=new, user_id=1,
        user_email='tester@shizuha.com')
    ItemActivity.objects.filter(pk=act.pk).update(created_at=at)
    return act


def _assign(item, at, email='tester@shizuha.com'):
    act = ItemActivity.objects.create(
        item=item, action='assigned', user_id=1, user_email=email)
    ItemActivity.objects.filter(pk=act.pk).update(created_at=at)
    return act


class QueueWaitsTests(TestCase):
    """PLAT-9077 M3: queue-wait from the durable assigned record + parked split."""

    def test_queue_wait_is_first_assignment_minus_created(self):
        from tasks.services.stage_metrics import queue_waits
        now = timezone.now()
        item = _make_item(title='assigned later', status='in_progress',
                          assignment_group='devops', now=now,
                          created_at=now - timedelta(hours=10))
        _assign(item, now - timedelta(hours=6))   # first assignment: 4h queue
        _assign(item, now - timedelta(hours=2))   # reassignment ignored

        data = queue_waits(days=30, now=now)
        row = next(r for r in data['rows'] if r['item_key'] == str(item.pk))
        self.assertAlmostEqual(row['queue_wait_seconds'], 4 * 3600, delta=5)
        self.assertTrue(row['assigned'])
        self.assertEqual(data['summary']['assigned_count'], 1)

    def test_never_assigned_item_is_honest_none_with_parking_dwell(self):
        from tasks.services.stage_metrics import queue_waits
        now = timezone.now()
        # PLAT-9115: never CREATE an Item directly in `blocked` (PLAT-4215
        # model invariant) — create in the legal initial status and
        # transition at the same backdated moment; the parked-dwell math
        # is unchanged (blocked since t-8h ⇒ 8h parked).
        item = _make_item(title='never picked', status='open',
                          assignment_group='devops', now=now,
                          created_at=now - timedelta(hours=8))
        _transition(item, 'open', 'blocked', now - timedelta(hours=8))

        data = queue_waits(days=30, now=now)
        row = next(r for r in data['rows'] if r['item_key'] == str(item.pk))
        self.assertIsNone(row['queue_wait_seconds'])
        self.assertFalse(row['assigned'])
        self.assertAlmostEqual(row['parked_seconds'], 8 * 3600, delta=5)
        self.assertEqual(data['summary']['unassigned_count'], 1)

    def test_parked_then_assigned_decomposes_blocked_dwell(self):
        from tasks.services.stage_metrics import queue_waits
        now = timezone.now()
        item = _make_item(title='parked first', status='in_progress',
                          assignment_group='devops', now=now,
                          created_at=now - timedelta(hours=10))
        # open → blocked at t-9h, blocked → open at t-7h (2h parked),
        # first assigned at t-6h (4h queue wait).
        _transition(item, 'open', 'blocked', now - timedelta(hours=9))
        _transition(item, 'blocked', 'open', now - timedelta(hours=7))
        _assign(item, now - timedelta(hours=6))

        data = queue_waits(days=30, now=now)
        row = next(r for r in data['rows'] if r['item_key'] == str(item.pk))
        self.assertAlmostEqual(row['queue_wait_seconds'], 4 * 3600, delta=5)
        self.assertAlmostEqual(row['parked_seconds'], 2 * 3600, delta=5)

    def test_alert_mode_excluded(self):
        from tasks.services.stage_metrics import queue_waits
        now = timezone.now()
        Item.objects.create(mode='alert', title='alert row', status='open')
        data = queue_waits(days=30, now=now)
        self.assertEqual(data['rows'], [])


class AgeDistributionTests(TestCase):
    """PLAT-9077 M5: current-state age buckets, terminal/alert excluded."""

    def test_buckets_land_by_age_and_exclude_terminal_and_alert(self):
        from tasks.services.stage_metrics import age_distribution
        now = timezone.now()
        _make_item(title='fresh', status='open', now=now,
                   created_at=now - timedelta(hours=12))          # le_1d
        _make_item(title='mid', status='in_progress', now=now,
                   created_at=now - timedelta(days=5))            # le_7d
        _make_item(title='old', status='needs_triage', now=now,
                   created_at=now - timedelta(days=45))           # gt_30d
        _make_item(title='done', status='completed', now=now,
                   created_at=now - timedelta(days=45))           # terminal: out
        Item.objects.create(mode='alert', title='alert', status='open',
                            created_at=now - timedelta(days=45))  # alert: out

        data = age_distribution(now=now)
        self.assertEqual(data['buckets']['le_1d'], 1)
        self.assertEqual(data['buckets']['le_7d'], 1)
        self.assertEqual(data['buckets']['gt_30d'], 1)
        self.assertEqual(data['total_open'], 3)

    def test_no_open_work_is_all_zero(self):
        from tasks.services.stage_metrics import age_distribution
        data = age_distribution()
        self.assertEqual(data['total_open'], 0)
        self.assertIsNone(data['oldest_created_at'])


class WipVsCapTests(TestCase):
    """PLAT-9077 M6: open assigned vs roster cap; cap-less seats honest."""

    def test_over_cap_and_within_cap_against_enforced_roster(self):
        from tasks.models import TeamRoster
        from tasks.services.stage_metrics import wip_vs_cap
        now = timezone.now()
        # Migrations 0055+0107 seed and WIP-enable a (shizuha, devops) roster
        # row in every migrated DB; a bare create violates
        # uniq_team_roster_org_name. Clear the seeded row so the fixture's
        # own enforced row is the only authority in this test.
        TeamRoster.objects.filter(team_name='devops').delete()
        TeamRoster.objects.create(
            team_name='devops', wip_enforced=True, active_wip_cap=1)

        a = _make_item(title='a', status='in_progress',
                       assignment_group='devops', now=now)
        b = _make_item(title='b', status='open',
                       assignment_group='devops', now=now)
        Item.objects.filter(pk=a.pk).update(assignee_email='busy@shizuha.com')
        Item.objects.filter(pk=b.pk).update(assignee_email='busy@shizuha.com')

        data = wip_vs_cap(now=now)
        row = next(r for r in data['rows']
                   if r['assignee_email'] == 'busy@shizuha.com')
        self.assertEqual(row['open_count'], 2)
        self.assertEqual(row['cap'], 1)
        self.assertTrue(row['over_cap'])
        self.assertEqual(data['summary']['over_cap_count'], 1)

    def test_cap_less_seat_is_honest_none(self):
        from tasks.models import TeamRoster
        from tasks.services.stage_metrics import wip_vs_cap
        now = timezone.now()
        # Migrations 0055 (pod-roster seed) + 0107 (PLS-509 platform WIP
        # enable) leave an ACTIVE, wip_enforced 'architecture' roster row
        # (active_wip_cap=1) in every migrated DB — the seat is not cap-less
        # there. Disable enforcement so this test exercises the genuine
        # cap-less path (no enforced roster row -> honest None).
        TeamRoster.objects.filter(team_name='architecture').update(
            wip_enforced=False)
        item = _make_item(title='free seat', status='in_progress',
                          assignment_group='architecture', now=now)
        Item.objects.filter(pk=item.pk).update(
            assignee_email='free@shizuha.com')

        data = wip_vs_cap(now=now)
        row = next(r for r in data['rows']
                   if r['assignee_email'] == 'free@shizuha.com')
        self.assertIsNone(row['cap'])
        self.assertFalse(row['over_cap'])
        self.assertEqual(data['summary']['seats_with_cap'], 0)

    def test_terminal_and_alert_excluded(self):
        from tasks.services.stage_metrics import wip_vs_cap
        now = timezone.now()
        done = _make_item(title='done', status='completed',
                          assignment_group='devops', now=now)
        Item.objects.filter(pk=done.pk).update(
            assignee_email='busy@shizuha.com')
        Item.objects.create(mode='alert', title='alert', status='open',
                            assignee_email='busy@shizuha.com')

        data = wip_vs_cap(now=now)
        self.assertEqual(data['rows'], [])


class CycleTimesP90Tests(TestCase):
    """PLAT-9077: p90 joins the cycle_times summary additively."""

    def test_summary_has_p90_and_keeps_p85_p95(self):
        from tasks.services.stage_metrics import cycle_times
        now = timezone.now()
        item = _make_item(title='shipped', status='completed',
                          assignment_group='devops', now=now,
                          created_at=now - timedelta(days=2))
        Item.objects.filter(pk=item.pk).update(completed_at=now)

        data = cycle_times(days=30, now=now)
        self.assertIn('p90_seconds', data['summary'])
        self.assertIn('p85_seconds', data['summary'])
        self.assertIn('p95_seconds', data['summary'])
        self.assertEqual(data['summary']['count'], 1)
        self.assertEqual(data['summary']['p90_seconds'],
                         data['summary']['p50_seconds'])
