from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from tasks.models import Item
from tasks.scheduler import KEY_RECURRING
from tasks.services.recurring_invariants import (
    SWEEP_LIVENESS_KEY,
    collect_recurring_invariant_metrics,
    render_prometheus_metrics,
)


class _NoopTimerManager:
    def schedule_item(self, item):
        return None


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.zsets = {}

    def get(self, key):
        return self.values.get(key)

    def zscore(self, key, member):
        return self.zsets.get(key, {}).get(str(member))


class RecurringInvariantMetricsTests(TestCase):
    def setUp(self):
        self.timer_patch = patch('tasks.scheduler.get_timer_manager', return_value=_NoopTimerManager())
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)
        self.now = timezone.now()

    def _item(self, title, **overrides):
        data = {
            'title': title,
            'mode': 'task',
            'status': 'in_progress',
            'is_recurring': True,
            'frequency': 'daily',
            'assignee_id': 101,
            'assignee_email': 'agent@example.com',
            'due_date': self.now - timedelta(days=2),
        }
        data.update(overrides)
        return Item.objects.create(**data)

    def test_collects_recurring_engine_invariant_counts(self):
        overdue = self._item('overdue recurring')
        healthy = self._item('healthy recurring', due_date=self.now + timedelta(hours=1))
        # Simulate a legacy/imported bad row: normal saves now reject this
        # configuration (PLAT-1256), but the invariant probe must still detect
        # pre-existing invalid data that bypassed model/serializer validation.
        Item.objects.bulk_create([Item(
            title='unschedulable recurring',
            mode='task',
            workflow_name='simple',
            status='in_progress',
            is_recurring=True,
            frequency='custom',
            recurrence_pattern='not parseable',
            assignee_id=101,
            assignee_email='agent@example.com',
            due_date=self.now - timedelta(days=2),
        )])
        self._item('unassigned recurring ignored', assignee_id=None, assignee_email='', due_date=self.now - timedelta(days=5))
        self._item(
            'CTX-478-shaped group queue', assignee_id=None, assignee_email='',
            assignment_group='engineering', due_date=self.now - timedelta(days=2),
            metadata={
                'recurring_owner_team': 'engineering',
                'recurring_cycle': {'assignment_andon_at': self.now.isoformat()},
            },
        )
        # Bypass workflow-status healing so the probe's stop-status filter is
        # tested directly against legacy/terminal data.
        Item.objects.bulk_create([Item(
            title='stopped recurring ignored',
            mode='task',
            workflow_name='simple',
            status='cancelled',
            is_recurring=True,
            frequency='daily',
            assignee_id=101,
            assignee_email='agent@example.com',
            due_date=self.now - timedelta(days=5),
        )])

        fake_redis = _FakeRedis()
        fake_redis.zsets[KEY_RECURRING] = {str(healthy.pk): healthy.due_date.timestamp()}
        fake_redis.values['pulse:leader:beat'] = 'pod-a:123'
        fake_redis.values['pulse:leader:scheduler'] = 'pod-b:456'

        metrics = collect_recurring_invariant_metrics(redis_client=fake_redis, now=self.now)

        self.assertEqual(metrics.overdue, 2)
        self.assertEqual(metrics.unschedulable, 1)
        self.assertEqual(metrics.missing_timer, 2)
        self.assertEqual(metrics.group_queued_missing_timer, 1)
        self.assertEqual(metrics.assignment_andon, 1)
        self.assertEqual(metrics.leader_counts, {'beat': 1, 'scheduler': 1})

        text = render_prometheus_metrics(metrics)
        self.assertIn('pulse_recurring_overdue 2', text)
        self.assertIn('pulse_recurring_unschedulable 1', text)
        self.assertIn('pulse_recurring_missing_timer 2', text)
        self.assertIn('pulse_recurring_group_queued_missing_timer 1', text)
        self.assertIn('pulse_recurring_assignment_andon 1', text)
        self.assertIn('pulse_recurring_redis_error 0', text)
        self.assertIn('pulse_leader_count{name="beat"} 1', text)

    def test_redis_error_reports_blind_not_fail_closed_total(self):
        """PLAT-9452: a RedisError sample must NOT masquerade as total timer loss.

        The old fail-closed path returned len(items) through the same gauge the
        PulseRecurringGroupQueueMissingTimer alert fires on, so a Redis blip on
        any replica rendered "every timer missing" (observed 09-20: per-replica
        counts oscillating 5→4→1→5 with max == total active population). The
        blind sample now reports 0 confirmed-missing and raises the dedicated
        pulse_recurring_redis_error gauge instead.
        """
        import redis as _redis

        group_item = self._item(
            'group queued blind sample', assignee_id=None, assignee_email='',
            assignment_group='engineering', due_date=self.now - timedelta(days=2),
        )

        class _BlindRedis(_FakeRedis):
            def zscore(self, key, member):
                raise _redis.RedisError('connection reset')

            def get(self, key):
                raise _redis.RedisError('connection reset')

        metrics = collect_recurring_invariant_metrics(redis_client=_BlindRedis(), now=self.now)

        self.assertEqual(metrics.missing_timer, 0)
        self.assertEqual(metrics.group_queued_missing_timer, 0)
        self.assertEqual(metrics.redis_error, 1)
        self.assertEqual(metrics.leader_counts, {'beat': 0, 'scheduler': 0})
        text = render_prometheus_metrics(metrics)
        self.assertIn('pulse_recurring_redis_error 1', text)
        self.assertIn('pulse_recurring_group_queued_missing_timer 0', text)
        self.assertIn('pulse_recurring_missing_timer 0', text)

    def test_rejected_recurring_item_not_counted_missing_timer(self):
        """A rejected recurring item is NOT a missing-timer (PLAT-6289).

        The fire handler (fire_due_cycle) treats `rejected` as a permanent stop
        and never re-fires it, so it cannot keep a recurring timer armed.
        Counting it as "missing timer" is a false positive that keeps
        PulseRecurringMissingTimer firing (observed DOJO-37, pk=25443).
        """
        Item.objects.bulk_create([Item(
            title='rejected recurring ignored',
            mode='task',
            workflow_name='simple',
            status='rejected',
            is_recurring=True,
            frequency='daily',
            assignee_id=101,
            assignee_email='agent@example.com',
            due_date=self.now - timedelta(days=2),
        )])

        fake_redis = _FakeRedis()
        metrics = collect_recurring_invariant_metrics(redis_client=fake_redis, now=self.now)

        self.assertEqual(metrics.missing_timer, 0)
        text = render_prometheus_metrics(metrics)
        self.assertIn('pulse_recurring_missing_timer 0', text)

    def test_routing_held_recurring_item_not_counted_missing_timer(self):
        """A PLS-528 routing-held recurring item is NOT a missing-timer (PLAT-9452).

        A held occurrence (recurrence-routing HOLD, e.g. reason=no_live_member)
        is intentionally un-routed and un-armed: the DB-driven due sweep owns
        its fire cadence (verified dispatching with delivery_retry_at, ichi
        09-20). A missing Redis timer is the designed state for a held item —
        counting it keeps PulseRecurringGroupQueueMissingTimer firing
        permanently on the hold population (live 09-20: detector 4 with
        ZCARD=0; keys 1451, 10450, 695 + 1).
        """
        Item.objects.bulk_create([Item(
            title='held recurring ignored',
            mode='task',
            workflow_name='simple',
            status='scheduled',
            is_recurring=True,
            frequency='daily',
            assignment_group='devops',
            metadata={'recurring_routing': {'discrepancy': {
                'active': True,
                'invalid_group': 'devops',
                'reason': 'no_live_member',
                'outcome': 'hold',
            }}},
            due_date=self.now - timedelta(days=2),
        )])

        fake_redis = _FakeRedis()
        metrics = collect_recurring_invariant_metrics(redis_client=fake_redis, now=self.now)

        self.assertEqual(metrics.missing_timer, 0)
        self.assertEqual(metrics.group_queued_missing_timer, 0)
        text = render_prometheus_metrics(metrics)
        self.assertIn('pulse_recurring_missing_timer 0', text)
        self.assertIn('pulse_recurring_group_queued_missing_timer 0', text)

    def test_routing_fallback_recurring_item_still_counted_missing_timer(self):
        """A resolved (fallback-routed) discrepancy is still signal-covered (PLAT-9452).

        Only outcome='hold' marks the designed un-armed state; a discrepancy
        whose occurrence was fallback-routed to a live team is genuinely
        routed and must keep missing-timer coverage.
        """
        Item.objects.bulk_create([Item(
            title='fallback-routed recurring counted',
            mode='task',
            workflow_name='simple',
            status='scheduled',
            is_recurring=True,
            frequency='daily',
            assignment_group='engineering',
            metadata={'recurring_routing': {'discrepancy': {
                'active': True,
                'invalid_group': 'devops',
                'reason': 'no_live_member',
                'outcome': 'fallback',
                'effective_group': 'engineering',
            }}},
            due_date=self.now - timedelta(days=2),
        )])

        fake_redis = _FakeRedis()
        metrics = collect_recurring_invariant_metrics(redis_client=fake_redis, now=self.now)

        self.assertEqual(metrics.missing_timer, 1)

    def test_done_recurring_template_still_counted_missing_timer(self):
        """A done-status recurring template with a continuing cycle stays
        signal-covered (PLAT-9452, live key 10450).

        The fire handler re-fires done templates whose recurrence continues
        (template reset semantics) — `done` is in neither RECURRING_STOP_STATUSES
        nor the fire-stop set, so the occurrence genuinely needs a timer and a
        missing one is real drift, not the designed un-armed state.
        """
        Item.objects.bulk_create([Item(
            title='done recurring template counted',
            mode='task',
            workflow_name='simple',
            status='done',
            is_recurring=True,
            frequency='daily',
            assignment_group='devops',
            due_date=self.now - timedelta(days=2),
        )])

        fake_redis = _FakeRedis()
        metrics = collect_recurring_invariant_metrics(redis_client=fake_redis, now=self.now)

        self.assertEqual(metrics.missing_timer, 1)

    def test_metrics_endpoint_uses_prometheus_text_exposition(self):
        fake_metrics = collect_recurring_invariant_metrics(redis_client=_FakeRedis(), now=self.now)
        with patch('tasks.services.recurring_invariants.collect_recurring_invariant_metrics', return_value=fake_metrics):
            resp = self.client.get('/api/metrics/')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'text/plain; version=0.0.4; charset=utf-8')
        self.assertIn(b'pulse_recurring_overdue', resp.content)


    def test_labeled_overdue_series_names_each_item(self):
        """PLS-1345: the labeled per-item series must name overdue culprits.

        PulseRecurringOverdue fired 2026-09-24 with 7 overdue items, but the
        scalar gauge names none and the permission-scoped items API hides
        cross-org/terminal rows by design — the runbook's per-item read was
        impossible from any seat. The exposition must carry
        ``pulse_recurring_overdue_item{item_key, organization_id,
        assignment_group}`` with one sample per overdue item, exactly as the
        runbook cites.
        """
        from tasks.models import Project, TeamRoster

        # org_id=1 maps to the canonical 'shizuha' slug (org_slug_for_project),
        # and the assignment org guard (PLAT-4213) requires the assignee's
        # roster org to match the task org when the item carries a project.
        project = Project.objects.create(name='Recurring Org', key='PLS', org_id=1)
        # Migrations seed org-scoped roster rows (uniq_team_roster_org_name;
        # e.g. (shizuha, engineering)) into every migrated DB — a bare create
        # violates the constraint. Clear the seeded row so the fixture's own
        # enforced roster is the only authority in this test (same pattern as
        # test_stage_metrics.WipVsCapTests).
        TeamRoster.objects.filter(
            team_name='engineering', organization_slug='shizuha').delete()
        TeamRoster.objects.create(
            team_name='engineering',
            organization_slug='shizuha',
            is_active=True,
            tiers=[['agent@example.com']],
        )
        grouped = self._item(
            'group-routed overdue', assignee_id=None, assignee_email='',
            assignment_group='engineering', due_date=self.now - timedelta(days=3),
        )
        org_scoped = self._item(
            'org-scoped overdue', due_date=self.now - timedelta(days=3),
            project=project,
        )
        healthy = self._item('healthy', due_date=self.now + timedelta(hours=1))

        metrics = collect_recurring_invariant_metrics(redis_client=_FakeRedis(), now=self.now)

        self.assertEqual(metrics.overdue, 2)
        self.assertEqual(
            {(ref.item_key, ref.organization_id, ref.assignment_group)
             for ref in metrics.overdue_items},
            # Project-less rows mint item_key lazily; the gauge falls back to pk.
            {(str(grouped.pk), '', 'engineering'),
             (str(org_scoped.item_key or org_scoped.pk), '1', '')},
        )

        text = render_prometheus_metrics(metrics)
        self.assertIn(
            f'pulse_recurring_overdue_item{{item_key="{grouped.pk}"'
            ',organization_id=""'
            ',assignment_group="engineering"} 1',
            text,
        )
        self.assertIn(
            f'pulse_recurring_overdue_item{{item_key="{org_scoped.item_key or org_scoped.pk}"'
            ',organization_id="1"'
            ',assignment_group=""} 1',
            text,
        )
        self.assertNotIn(f'item_key="{healthy.item_key}"', text)
        # The scalar stays the count of record and matches sample count.
        self.assertIn('pulse_recurring_overdue 2', text)
        self.assertEqual(text.count('\npulse_recurring_overdue_item{'), 2)

    def test_labeled_overdue_series_header_only_when_none_overdue(self):
        """PLS-1345: no overdue items → header-only family, zero samples."""
        self._item('healthy recurring', due_date=self.now + timedelta(hours=1))
        metrics = collect_recurring_invariant_metrics(redis_client=_FakeRedis(), now=self.now)
        self.assertEqual(metrics.overdue, 0)
        self.assertEqual(metrics.overdue_items, ())
        text = render_prometheus_metrics(metrics)
        self.assertIn('# TYPE pulse_recurring_overdue_item gauge', text)
        self.assertNotIn('\npulse_recurring_overdue_item{', text)

    def test_labeled_overdue_series_escapes_label_values(self):
        """Label values are escaped per exposition format (backslash/quote/newline)."""
        from tasks.services.recurring_invariants import (
            OverdueRecurringItemRef,
            RecurringInvariantMetrics,
            render_prometheus_metrics,
        )

        metrics = RecurringInvariantMetrics(
            overdue=1,
            unschedulable=0,
            missing_timer=0,
            group_queued_missing_timer=0,
            assignment_andon=0,
            leader_counts={},
            redis_error=0,
            overdue_items=(OverdueRecurringItemRef(
                item_key='X-1 "\\n', organization_id='7', assignment_group='a"b',
            ),),
        )
        text = render_prometheus_metrics(metrics)
        self.assertIn(
            'pulse_recurring_overdue_item{item_key="X-1 \\"\\\\n"'
            ',organization_id="7"'
            ',assignment_group="a\\"b"} 1',
            text,
        )


class _BlindRedis(_FakeRedis):
    """A fake whose GET path raises — the Redis-blind sample shape."""

    def get(self, key):
        import redis as _redis
        raise _redis.RedisError('connection pool stalled')


class SweepLivenessGaugeTests(TestCase):
    """PLAT-9452 defect 3: the sweep-liveness gauge contract.

    ``pulse_sweep_last_success_timestamp_seconds`` distinguishes "beat firing
    but healer no-op" from "beat dead". Key absent → 0.0 (never completed,
    maximally stale, truth-bearing); Redis read failure → the family is
    OMITTED (never synthetic zero — omit-don't-zero contract).
    """

    def setUp(self):
        self.now = timezone.now()

    def test_collect_reads_liveness_key(self):
        fake = _FakeRedis()
        fake.values[SWEEP_LIVENESS_KEY] = str(self.now.timestamp())
        metrics = collect_recurring_invariant_metrics(redis_client=fake, now=self.now)
        self.assertEqual(metrics.sweep_last_success_timestamp, self.now.timestamp())

    def test_missing_liveness_key_renders_zero_never_ran(self):
        metrics = collect_recurring_invariant_metrics(redis_client=_FakeRedis(), now=self.now)
        self.assertEqual(metrics.sweep_last_success_timestamp, 0.0)

    def test_redis_error_omits_family_none(self):
        metrics = collect_recurring_invariant_metrics(redis_client=_BlindRedis(), now=self.now)
        self.assertIsNone(metrics.sweep_last_success_timestamp)

    def test_render_includes_liveness_gauge(self):
        fake = _FakeRedis()
        fake.values[SWEEP_LIVENESS_KEY] = '1789904607.5'
        metrics = collect_recurring_invariant_metrics(redis_client=fake, now=self.now)
        text = render_prometheus_metrics(metrics)
        self.assertIn('pulse_sweep_last_success_timestamp_seconds 1789904607.5', text)
        self.assertIn('# TYPE pulse_sweep_last_success_timestamp_seconds gauge', text)

    def test_render_omits_family_on_probe_failure(self):
        metrics = collect_recurring_invariant_metrics(redis_client=_BlindRedis(), now=self.now)
        text = render_prometheus_metrics(metrics)
        self.assertNotIn('pulse_sweep_last_success_timestamp_seconds', text)

    def test_render_zero_when_sweep_never_ran(self):
        metrics = collect_recurring_invariant_metrics(redis_client=_FakeRedis(), now=self.now)
        text = render_prometheus_metrics(metrics)
        self.assertIn('pulse_sweep_last_success_timestamp_seconds 0.0', text)

    def test_liveness_key_is_the_shared_constant(self):
        """Sweep (writer) and detector (reader) must agree on the key."""
        from tasks.services.recurring_invariants import SWEEP_LIVENESS_KEY as imported
        self.assertEqual(imported, 'pulse:sweep:last_success_ts')