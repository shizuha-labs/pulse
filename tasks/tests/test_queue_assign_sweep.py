"""The group-assign sweep saves the resolver's answer.

awaiting_merge is in the sweep. An urgent miss is retried next pass.
A non-urgent miss waits five minutes. The row lock is taken only to write,
so a person claimed between resolve and save is left in place.
"""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import sweep_assign_group_queued


class _MemRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.values[key] = value


class QueueAssignSweepTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.member = users.objects.create_user(
            username='qas-member', email='ryo@shizuha.com', password='x',
        )
        self.other = users.objects.create_user(
            username='qas-other', email='kei@shizuha.com', password='x',
        )
        self.project = Project.objects.create(
            name='Queue assign', key='QAS', org_id=1, access_level='open',
        )
        TeamRoster.objects.update_or_create(
            team_name='merge',
            organization_id=1,
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.member.email, self.other.email]],
                'is_active': True,
            },
        )
        self.redis = _MemRedis()

    def _item(self, *, status, priority):
        item = Item.objects.create(
            project=self.project,
            title=f'{status} {priority}',
            mode='task',
            item_type='task',
            status='todo',
            workflow_status='To Do',
            assignment_group='merge',
            priority=priority,
        )
        Item.objects.filter(pk=item.pk).update(
            status=status, workflow_status=status, priority=priority,
        )
        item.refresh_from_db()
        return item

    def _run(self):
        with patch.object(WorkflowService, '_redis_client', return_value=self.redis):
            return sweep_assign_group_queued.run()

    def test_awaiting_merge_is_seated_from_the_resolver(self):
        item = self._item(status='awaiting_merge', priority='urgent')

        def _apply(target, uid, email, fields):
            target.assignee_id = uid
            target.assignee_email = email
            fields.extend(['assignee_id', 'assignee_email'])
            return uid, email

        with patch.object(
            WorkflowService, '_resolve_team_assignee',
            return_value=(self.member.id, self.member.email),
        ), patch.object(
            WorkflowService, '_apply_resolved_assignee', side_effect=_apply,
        ):
            result = self._run()

        item.refresh_from_db()
        self.assertEqual(result['assigned'], 1)
        self.assertEqual(item.assignee_email, self.member.email)
        self.assertEqual(result['queued'], 1)

    def test_urgent_miss_is_not_remembered(self):
        item = self._item(status='todo', priority='urgent')
        with patch.object(
            WorkflowService, '_resolve_team_assignee', return_value=(None, None),
        ):
            result = self._run()
        self.assertEqual(result['urgent_unseated'], 1)
        stored = json.loads(self.redis.values.get('pulse:queue_assign_skip_until') or '{}')
        self.assertNotIn(str(item.pk), stored)

    def test_non_urgent_miss_waits_five_minutes(self):
        item = self._item(status='todo', priority='normal')
        with patch.object(
            WorkflowService, '_resolve_team_assignee', return_value=(None, None),
        ):
            result = self._run()
        self.assertEqual(result['urgent_unseated'], 0)
        self.assertEqual(result['capability_mismatch'], 1)
        stored = json.loads(self.redis.values['pulse:queue_assign_skip_until'])
        self.assertIn(str(item.pk), stored)

    def test_assignee_claimed_before_save_is_kept(self):
        item = self._item(status='todo', priority='urgent')

        def _claim(*_args, **_kwargs):
            Item.objects.filter(pk=item.pk).update(
                assignee_id=self.other.id,
                assignee_email=self.other.email,
            )
            return self.member.id, self.member.email

        with patch.object(
            WorkflowService, '_resolve_team_assignee', side_effect=_claim,
        ):
            result = self._run()

        item.refresh_from_db()
        self.assertEqual(result['assigned'], 0)
        self.assertEqual(item.assignee_email, self.other.email)


class QueueAssignSweepBudgetTests(QueueAssignSweepTests):
    """PLS-1415: the periodic pass must bound its wall-clock occupancy of the
    worker slot. With a large group-queue backlog, one unbounded pass held the
    single-slot fork worker ~25 minutes while every other sweep (recurring
    timers, escalation outbox, org health, liveness probes) queued behind it —
    pass duration ballooned past the 300s bound and fired
    PulseSweepLivenessStale per replica. The budget defers, never drops."""

    def _run_with_budget(self, max_seconds, monotonic=None):
        """Run with the resolver + applier patched (mirroring the parent
        class's working tests): in the hermetic CI sandbox the real resolver
        cannot reach the id plane and would unseat urgent items with
        'resolver source unavailable' instead of assigning — assigned=0 with
        no test-level fault in the budget logic itself. ``monotonic`` is an
        optional callable patched over tasks.tasks.time.monotonic."""
        def _apply(target, uid, email, fields):
            target.assignee_id = uid
            target.assignee_email = email
            fields.extend(['assignee_id', 'assignee_email'])
            return uid, email

        with patch.object(
            WorkflowService, '_resolve_team_assignee',
            return_value=(self.member.id, self.member.email),
        ), patch.object(
            WorkflowService, '_apply_resolved_assignee', side_effect=_apply,
        ):
            with patch.object(WorkflowService, '_redis_client', return_value=self.redis):
                if monotonic is None:
                    return sweep_assign_group_queued.run(max_seconds=max_seconds)
                with patch('tasks.tasks.time.monotonic', side_effect=monotonic):
                    return sweep_assign_group_queued.run(max_seconds=max_seconds)

    def test_budget_defers_remaining_items_without_dropping_them(self):
        item_a = self._item(status='todo', priority='urgent')
        item_b = self._item(status='todo', priority='normal')
        # monotonic call order: 1 for the deadline base, then one check per
        # queued item (sorted urgent-first). Base 100.0 + budget 0.5 → item_a
        # is examined (check 100.1), item_b's check (101.0) trips the budget.
        # A counter-based fake (not a fixed side_effect list): unrelated
        # internals may call time.monotonic during item_a's assignment, and a
        # fixed list would shift the budget-check sequence → StopIteration.
        # Calls beyond the scripted three clamp to the expired-clock value so
        # any extra budget check still reports exhaustion.
        scripted = iter([100.0, 100.1, 101.0])

        def fake_monotonic():
            return next(scripted, 101.0)

        result = self._run_with_budget(0.5, monotonic=fake_monotonic)
        self.assertEqual(result['assigned'], 1)
        self.assertEqual(result['examined'], 1)
        self.assertTrue(result['budget_exhausted'])
        item_a.refresh_from_db()
        item_b.refresh_from_db()
        self.assertEqual(item_a.assignee_email, self.member.email)
        self.assertIsNone(item_b.assignee_email)

    def test_zero_budget_disables_the_bound(self):
        self._item(status='todo', priority='urgent')
        result = self._run_with_budget(0)
        self.assertFalse(result['budget_exhausted'])
        self.assertEqual(result['assigned'], 1)

    def test_periodic_default_comes_from_env(self):
        self._item(status='todo', priority='urgent')
        with patch.dict('os.environ', {'PULSE_QUEUE_ASSIGN_MAX_SECONDS': '7'}):
            result = self._run_with_budget(None)
        self.assertEqual(result['max_seconds'], 7.0)
        self.assertFalse(result['budget_exhausted'])

    def test_result_contract_reports_budget_fields(self):
        self._item(status='todo', priority='urgent')
        result = self._run_with_budget(120)
        for key in ('examined', 'budget_exhausted', 'max_seconds', 'queued'):
            self.assertIn(key, result)
