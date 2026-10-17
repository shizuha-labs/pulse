import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from tasks.models import Comment, Item, ItemActivity, Project, TeamRoster
from tasks.services.notifications import notification_service
from tasks.services.recurring_cycle_state import (
    RecurringCycleError,
    complete_cycle,
    cycle_snapshot,
    manual_start_cycle,
)
from tasks.tasks import fire_recurring_due
from .base import create_blocked_item, satisfy_blocked_invariant

User = get_user_model()


@override_settings(SHIZUHA_CONNECT_API_URL='http://connect.test/api')
class RecurringCycleStaleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='rin', email='rin@shizuha.com', password='x')
        self.alt = User.objects.create_user(username='ryo', email='ryo@shizuha.com', password='x')
        self.project = Project.objects.create(
            name='Recurring cycle test',
            key='P214',
            org_id=1,
        )
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={
                'organization_id': 1,
                'tiers': [['rin@shizuha.com', 'ryo@shizuha.com']],
                'is_active': True,
            },
        )

    def _item(self, **kw):
        kw.setdefault('status', 'open')
        kw.setdefault('due_date', timezone.now() - timedelta(minutes=1))
        kw.setdefault('assignment_group', 'engineering')
        factory = create_blocked_item if kw.get('status') == 'blocked' else Item.objects.create
        return factory(
            project=self.project,
            title='daily sweep', mode='task',
            workflow_name='autonomous-dev',
            assignee_id=self.user.id, assignee_email='rin@shizuha.com',
            is_recurring=True, frequency='daily',
            **kw,
        )

    def test_skip_count_accrues_and_resets_on_terminal_fire(self):
        item = self._item()
        first_due = item.due_date

        fire_recurring_due(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.metadata['recurring_cycle']['consecutive_skips'], 1)
        self.assertGreater(item.due_date, first_due)

        item.due_date = timezone.now() - timedelta(minutes=1)
        item.save(update_fields=['due_date'])
        fire_recurring_due(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.metadata['recurring_cycle']['consecutive_skips'], 2)
        self.assertTrue(Comment.objects.filter(item=item, content__icontains='missed 2 consecutive').exists())
        self.assertTrue(ItemActivity.objects.filter(item=item, action='recurring_cycle_stale').exists())

        item.status = 'done'
        item.due_date = timezone.now() - timedelta(minutes=1)
        item.save(update_fields=['status', 'due_date'])
        with mock.patch('requests.post'):
            fire_recurring_due(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.metadata['recurring_cycle']['consecutive_skips'], 0)
        self.assertIsNotNone(item.metadata['recurring_cycle']['last_terminal_completion_at'])

    def test_n3_escalation_fires_once_per_crossing(self):
        item = self._item(metadata={'recurring_cycle': {'consecutive_skips': 2}})
        # Assert on the escalation notifier itself, not the low-level requests.post:
        # the reassignment (assignee bounce) and the escalation Comment also emit
        # their own notification posts, so the raw post count is not "exactly the
        # escalation" (and it varies with warmed caches across the suite). Mocking
        # _notify_recurring_cycle_team_lead pins the "fires once per crossing" intent.
        with mock.patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee', return_value=(self.alt.id, 'ryo@shizuha.com')) as resolve, \
             mock.patch('tasks.tasks._notify_recurring_cycle_team_lead', return_value=True) as notify, \
             mock.patch('requests.post'):
            fire_recurring_due(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.metadata['recurring_cycle']['escalated_at_skips'], 3)
        self.assertEqual(item.assignee_email, 'ryo@shizuha.com')
        resolve.assert_any_call(
            'engineering', exclude_email='rin@shizuha.com', enforce_cap=False,
            project_key='P214', item=item,
        )
        notify.assert_called_once()
        self.assertTrue(Comment.objects.filter(item=item, content__icontains='recurring-cycle escalation').exists())

        item.due_date = timezone.now() - timedelta(minutes=1)
        item.save(update_fields=['due_date'])
        with mock.patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee') as resolve2, \
             mock.patch('requests.post') as post2:
            fire_recurring_due(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.metadata['recurring_cycle']['consecutive_skips'], 4)
        resolve2.assert_not_called()
        post2.assert_not_called()

    def test_recurring_assignment_notification_carries_cycle_fence(self):
        due = timezone.now() + timedelta(hours=8)
        item = self._item(
            status='open', due_date=due,
            metadata={'recurring_cycle': {
                'cycle_revision': 13,
                'active_generation': 5,
                'completed_generation': 4,
                'last_terminal_completion_at': '2026-07-10T09:45:00+00:00',
            }},
        )

        with mock.patch.object(notification_service, 'notify_item_event', return_value={}), \
             mock.patch('requests.post') as post:
            notification_service.notify_assignment_changed(item, assigned_by='scheduler@pulse.shizuha.com')

        content = post.call_args.kwargs['json']['content']
        state_line = next(line for line in content.splitlines() if line.startswith('recurring_event='))
        event = json.loads(state_line.split('=', 1)[1])
        self.assertEqual(event['cycle_revision'], 13)
        self.assertEqual(event['generation'], 5)
        self.assertEqual(event['due_boundary'], due.isoformat())
        self.assertEqual(event['completion_watermark'], '2026-07-10T09:45:00+00:00')

    def test_backlog_lift_resets_skips_but_blocked_counts_without_lift(self):
        """PLS-1291: a fire that lifts a Backlog-resident item is the cadence
        WORKING — the lift resets consecutive_skips/escalated_at_skips (run-close
        hygiene re-parks healthy recurring items to Backlog after each run).
        Blocked items without a lift still accrue."""
        backlog = self._item(status='backlog')
        result = fire_recurring_due(backlog.pk)
        backlog.refresh_from_db()
        self.assertIn('Lifted recurring', result)
        self.assertEqual(backlog.status, 'open')
        self.assertEqual(backlog.metadata['recurring_cycle']['consecutive_skips'], 0)
        self.assertEqual(backlog.metadata['recurring_cycle']['escalated_at_skips'], 0)
        # PLS-1286/PLS-1291: the lift path is NOT a missed fire — no period-1
        # signal, no assignee missed-fire notification.
        self.assertFalse(Comment.objects.filter(
            item=backlog, content__contains='[recurring-cycle] missed fire: period 1',
        ).exists())

        blocked = self._item(status='blocked')
        satisfy_blocked_invariant(blocked)
        fire_recurring_due(blocked.pk)
        blocked.refresh_from_db()
        self.assertEqual(blocked.status, 'blocked')
        self.assertEqual(blocked.metadata['recurring_cycle']['consecutive_skips'], 1)

    def test_healthy_backlog_resident_cadence_never_escalates(self):
        """PLS-1291 regression (PLAT-528 item-2014 shape): a weekly cadence whose
        run-close hygiene re-parks the item to Backlog after every run must
        never accrue skips, never fire the period-1 signal, and never bounce
        the assignee — the lift resets the counters at every fire."""
        item = self._item(status='backlog')
        for _ in range(6):
            fire_recurring_due(item.pk)
            item.refresh_from_db()
            self.assertEqual(item.status, 'open')
            self.assertEqual(item.metadata['recurring_cycle']['consecutive_skips'], 0)
            # Simulate run-close hygiene re-parking the item for the next period.
            item.status = 'backlog'
            item.due_date = timezone.now() - timedelta(minutes=1)
            item.save(update_fields=['status', 'due_date'])
        self.assertFalse(Comment.objects.filter(
            item=item, content__icontains='recurring-cycle escalation',
        ).exists())
        self.assertFalse(Comment.objects.filter(
            item=item, content__contains='[recurring-cycle] missed fire: period 1',
        ).exists())
        self.assertEqual(item.assignee_email, 'rin@shizuha.com')

    def test_stop_statuses_still_stop(self):
        item = self._item(status='deferred')
        result = fire_recurring_due(item.pk)
        item.refresh_from_db()
        self.assertIn('stopped', result)
        self.assertNotIn('recurring_cycle', item.metadata or {})

    def test_group_queued_unassigned_driver_retries_then_andons_with_timer_state(self):
        item = Item.objects.create(
            project=self.project,
            item_key='CTX-478-REGRESSION',
            title='Recurring Cortex TTFT guard',
            mode='task',
            workflow_name='autonomous-dev',
            status='open',
            assignment_group='devops',
            assignee_id=None,
            assignee_email='',
            is_recurring=True,
            frequency='daily',
            due_date=timezone.now() - timedelta(minutes=1),
            metadata={'recurring_owner_team': 'engineering'},
        )

        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            return_value=(None, None),
        ), mock.patch('tasks.tasks._notify_recurring_assignment_andon', return_value=True) as notify:
            for expected in (1, 2, 3):
                result = fire_recurring_due(item.pk)
                item.refresh_from_db()
                cycle = item.metadata['recurring_cycle']
                self.assertEqual(item.status, 'scheduled')
                self.assertEqual(item.assignment_group, 'engineering')
                self.assertIsNone(item.assignee_id)
                self.assertEqual(cycle['assignment_retry_attempts'], expected)
                self.assertGreater(item.due_date, timezone.now())
                if expected < 3:
                    self.assertIn('assignment_retry', result)
                    item.due_date = timezone.now() - timedelta(minutes=1)
                    item.save(update_fields=['due_date'])
                else:
                    self.assertIn('assignment_andon', result)

        notify.assert_called_once_with(item, 'engineering', 3)
        self.assertTrue(item.metadata['recurring_cycle']['assignment_andon_at'])
        self.assertEqual(
            ItemActivity.objects.filter(item=item, action='recurring_assignment_andon').count(),
            1,
        )
        self.assertEqual(
            Comment.objects.filter(item=item, content__contains='recurring-assignment ANDON').count(),
            1,
        )

        # A later natural-boundary attempt stays bounded: no second ANDON or
        # notification, and the next timer boundary remains in the future.
        item.due_date = timezone.now() - timedelta(minutes=1)
        item.save(update_fields=['due_date'])
        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            return_value=(None, None),
        ), mock.patch('tasks.tasks._notify_recurring_assignment_andon') as repeat_notify:
            fire_recurring_due(item.pk)
        item.refresh_from_db()
        repeat_notify.assert_not_called()
        self.assertGreater(item.due_date, timezone.now())
        self.assertEqual(
            ItemActivity.objects.filter(item=item, action='recurring_assignment_andon').count(),
            1,
        )

    def test_group_queued_driver_opens_once_when_owner_resolves(self):
        item = Item.objects.create(
            project=self.project,
            item_key='CTX-478-RECOVERY',
            title='Recurring Cortex TTFT guard', mode='task',
            workflow_name='autonomous-dev', status='scheduled',
            assignment_group='engineering', assignee_id=None, assignee_email='',
            is_recurring=True, frequency='daily',
            due_date=timezone.now() - timedelta(minutes=1),
            metadata={'recurring_owner_team': 'engineering', 'recurring_cycle': {
                'assignment_retry_attempts': 3,
                'assignment_andon_at': timezone.now().isoformat(),
            }},
        )
        def apply_owner(locked_item, uid, email, fields, **kwargs):
            locked_item.assignee_id = self.user.id
            locked_item.assignee_email = self.user.email
            fields.extend(['assignee_id', 'assignee_email'])

        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            return_value=(self.user.id, self.user.email),
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService._apply_resolved_assignee',
            side_effect=apply_owner,
        ), mock.patch('requests.post'):
            result = fire_recurring_due(item.pk)

        item.refresh_from_db()
        self.assertIn('Fired recurring', result)
        self.assertEqual(item.assignee_id, self.user.id)
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertNotEqual(item.status, 'scheduled')
        self.assertEqual(item.metadata['recurring_cycle']['assignment_retry_attempts'], 0)
        self.assertIsNone(item.metadata['recurring_cycle']['assignment_andon_at'])

    def test_backfill_command_idempotent_and_lifts_backlog(self):
        item = self._item(item_key='PLAT-250', status='backlog')
        call_command('backfill_recurring_cycle_counters', keys=['PLAT-250'])
        item.refresh_from_db()
        self.assertEqual(item.status, 'open')
        self.assertEqual(item.metadata['recurring_cycle']['consecutive_skips'], 2)
        first_activity_count = ItemActivity.objects.filter(item=item, action='recurring_cycle_fired').count()

        call_command('backfill_recurring_cycle_counters', keys=['PLAT-250'])
        item.refresh_from_db()
        self.assertEqual(item.metadata['recurring_cycle']['consecutive_skips'], 2)
        self.assertEqual(ItemActivity.objects.filter(item=item, action='recurring_cycle_fired').count(), first_activity_count)

    def test_manual_start_is_assignee_only_cas_and_replay_idempotent(self):
        due = timezone.now() + timedelta(hours=8)
        item = self._item(
            status='scheduled', due_date=due,
            metadata={'recurring_cycle': {
                'cycle_revision': 7,
                'active_generation': 3,
                'completed_generation': 3,
                'last_terminal_completion_at': timezone.now().isoformat(),
            }},
        )
        event_id = 'pls419-manual-start-7'
        first = manual_start_cycle(
            item.pk,
            user=self.user,
            expected_cycle_revision=7,
            expected_generation=3,
            expected_due_at=due.isoformat(),
            request_event_id=event_id,
        )
        item.refresh_from_db()
        self.assertTrue(first.applied)
        self.assertEqual(item.metadata['recurring_cycle']['active_generation'], 4)
        self.assertEqual(item.metadata['recurring_cycle']['cycle_revision'], 8)
        self.assertGreater(item.due_date, due)
        self.assertEqual(
            ItemActivity.objects.filter(item=item, action='recurring_cycle_fired_manual').count(),
            1,
        )

        replay = manual_start_cycle(
            item.pk,
            user=self.user,
            expected_cycle_revision=7,
            expected_generation=3,
            expected_due_at=due.isoformat(),
            request_event_id=event_id,
        )
        self.assertTrue(replay.replay)
        self.assertEqual(
            ItemActivity.objects.filter(item=item, action='recurring_cycle_fired_manual').count(),
            1,
        )

        other = self._item(status='scheduled', due_date=due)
        with self.assertRaisesRegex(RecurringCycleError, 'assignee-only'):
            manual_start_cycle(other.pk, user=self.alt, expected_cycle_revision=0)

    def test_delayed_stale_actor_after_newer_completion_is_noop(self):
        due = timezone.now() + timedelta(hours=8)
        item = self._item(
            status='open', due_date=due,
            metadata={'recurring_cycle': {
                'cycle_revision': 12,
                'active_generation': 5,
                'completed_generation': 4,
                'consecutive_skips': 3,
                'escalated_at_skips': 3,
            }},
        )
        stale = cycle_snapshot(item)
        completed = complete_cycle(
            item.pk, actor_id=self.user.id, actor_email=self.user.email,
        )
        self.assertTrue(completed.applied)
        item.refresh_from_db()
        completion_due = item.due_date
        completion_revision = item.metadata['recurring_cycle']['cycle_revision']
        completion_activity_count = ItemActivity.objects.filter(item=item).count()

        with self.assertRaisesRegex(RecurringCycleError, 'stale'):
            manual_start_cycle(
                item.pk,
                user=self.user,
                expected_cycle_revision=stale['cycle_revision'],
                expected_generation=stale['generation'],
                expected_due_at=stale['due_boundary'],
                expected_completion_watermark=stale['completion_watermark'],
                request_event_id=stale['event_id'],
            )
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')
        self.assertEqual(item.due_date, completion_due)
        self.assertEqual(item.assignee_email, self.user.email)
        self.assertEqual(item.metadata['recurring_cycle']['cycle_revision'], completion_revision)
        self.assertEqual(item.metadata['recurring_cycle']['consecutive_skips'], 0)
        self.assertEqual(ItemActivity.objects.filter(item=item).count(), completion_activity_count)

    def test_old_queued_scheduler_snapshot_fires_only_once(self):
        old_due = timezone.now() - timedelta(minutes=1)
        item = self._item(
            status='done', due_date=old_due,
            metadata={'recurring_cycle': {
                'cycle_revision': 2,
                'active_generation': 6,
                'completed_generation': 6,
            }},
        )
        with mock.patch('requests.post'):
            first = fire_recurring_due(
                item.pk,
                expected_cycle_revision=2,
                expected_generation=6,
                expected_due_at=old_due.isoformat(),
            )
        item.refresh_from_db()
        after_due = item.due_date
        after_revision = item.metadata['recurring_cycle']['cycle_revision']
        after_activity_count = ItemActivity.objects.filter(item=item).count()
        self.assertIn('Fired recurring', first)

        duplicate = fire_recurring_due(
            item.pk,
            expected_cycle_revision=2,
            expected_generation=6,
            expected_due_at=old_due.isoformat(),
        )
        item.refresh_from_db()
        self.assertIn('stale fire', duplicate)
        self.assertEqual(item.due_date, after_due)
        self.assertEqual(item.metadata['recurring_cycle']['cycle_revision'], after_revision)
        self.assertEqual(ItemActivity.objects.filter(item=item).count(), after_activity_count)

    def _recurrence_route_resolver(self, team, *args, **kwargs):
        return {
            'engineering': (self.alt.id, self.alt.email),
            'marketing': (self.user.id, self.user.email),
            'disabled-team': (self.user.id, self.user.email),
            'capability-team': (self.user.id, self.user.email),
        }.get(team, (None, None))

    def _finish_and_make_due(self, item):
        completed = complete_cycle(item.pk)
        self.assertTrue(completed.applied)
        item.refresh_from_db()
        item.due_date = timezone.now() - timedelta(minutes=1)
        item.save(update_fields=['due_date'])

    @mock.patch('requests.post')
    @mock.patch(
        'tasks.services.workflow_service.WorkflowService._member_can_execute_from_status',
        return_value=True,
    )
    @mock.patch(
        'tasks.services.workflow_service.WorkflowService._seat_satisfies_required_capability',
        return_value=True,
    )
    @mock.patch(
        'tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment',
        return_value=True,
    )
    def test_zero_member_template_falls_back_once_then_restores_and_rearms(
            self, _available, _capable, _can_execute, _post):
        """Production order: generate -> complete -> generate across four cycles."""
        roster = TeamRoster.objects.create(
            team_name='marketing', organization_slug='shizuha',
            organization_id=1, tiers=[[self.user.email]], is_active=True,
        )
        item = self._item(
            status='scheduled',
            assignment_group='marketing',
            metadata={
                'recurring_owner_team': 'marketing',
                'recurring_routing': {
                    'fallback_assignment_group': 'engineering',
                },
            },
        )
        roster.tiers = [[]]
        roster.save(update_fields=['tiers', 'updated_at'])

        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            side_effect=self._recurrence_route_resolver,
        ):
            first = fire_recurring_due(item.pk)
            item.refresh_from_db()
            self.assertIn('Fired recurring', first)
            self.assertEqual(item.assignment_group, 'engineering')
            self.assertEqual(item.assignee_email, self.alt.email)
            self.assertEqual(item.metadata['recurring_owner_team'], 'engineering')
            self.assertEqual(
                item.metadata['recurring_routing']['discrepancy']['invalid_group'],
                'marketing',
            )
            self.assertEqual(
                Comment.objects.filter(
                    item=item,
                    content__contains='PLS-528 recurrence-routing alert',
                ).count(),
                1,
            )

            # The same invalid roster condition on the next real caller cycle
            # reuses the fallback and does not emit one alert per cycle.
            self._finish_and_make_due(item)
            second = fire_recurring_due(item.pk)
            item.refresh_from_db()
            self.assertIn('Fired recurring', second)
            self.assertEqual(item.assignment_group, 'engineering')
            self.assertEqual(
                Comment.objects.filter(
                    item=item,
                    content__contains='PLS-528 recurrence-routing alert',
                ).count(),
                1,
            )

            # A live roster repair clears the discrepancy and restores the
            # declared template team on the following generation.
            self._finish_and_make_due(item)
            roster.tiers = [[self.user.email]]
            roster.save(update_fields=['tiers', 'updated_at'])
            third = fire_recurring_due(item.pk)
            item.refresh_from_db()
            self.assertIn('Fired recurring', third)
            self.assertEqual(item.assignment_group, 'marketing')
            self.assertFalse(
                item.metadata['recurring_routing']['discrepancy']['active'],
            )

            # A later roster state change recreates the condition and re-arms
            # exactly one new alert, rather than carrying a forever-suppressed bit.
            self._finish_and_make_due(item)
            roster.tiers = [[]]
            roster.save(update_fields=['tiers', 'updated_at'])
            fourth = fire_recurring_due(item.pk)
            item.refresh_from_db()
            self.assertIn('Fired recurring', fourth)
            self.assertEqual(item.assignment_group, 'engineering')
            self.assertEqual(
                Comment.objects.filter(
                    item=item,
                    content__contains='PLS-528 recurrence-routing alert',
                ).count(),
                2,
            )

    @mock.patch('requests.post')
    @mock.patch(
        'tasks.services.workflow_service.WorkflowService._member_can_execute_from_status',
        return_value=True,
    )
    @mock.patch(
        'tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment',
        return_value=True,
    )
    def test_deleted_disabled_and_capability_incompatible_templates_use_live_fallback(
            self, _available, _can_execute, _post):
        cases = (
            ('deleted-team', None, 'deleted'),
            ('disabled-team', {'tiers': [[self.user.email]], 'is_active': False}, 'disabled'),
            ('capability-team', {'tiers': [[self.user.email]], 'is_active': True}, 'capability_incompatible'),
        )
        for group, roster_config, expected_reason in cases:
            with self.subTest(group=group):
                roster = TeamRoster.objects.create(
                    team_name=group, organization_slug='shizuha',
                    organization_id=1, tiers=[[self.user.email]], is_active=True,
                )
                item = self._item(
                    status='scheduled',
                    assignment_group=group,
                    labels=(
                        {'needs:kubeconfig': True}
                        if group == 'capability-team' else {}
                    ),
                    metadata={
                        'recurring_owner_team': group,
                        'recurring_routing': {
                            'fallback_assignment_group': 'engineering',
                        },
                    },
                )
                if roster_config is None:
                    roster.delete()
                elif not roster_config['is_active']:
                    roster.is_active = False
                    roster.save(update_fields=['is_active', 'updated_at'])

                def capable(email, required, item=None, **kwargs):
                    # main's verdict passes item= (PLS-278 composition)
                    return not required or email == self.alt.email

                # main's _apply_resolved_assignee resolves the fallback seat's
                # identity via shizuha-id (blackholed in tests) — mock the
                # by-email fetch the way test_assignment.py does.
                def _by_email(email, *args, **kwargs):
                    for u in (self.user, self.alt):
                        if u.email.strip().lower() == (email or '').strip().lower():
                            return {'id': u.id, 'email': u.email,
                                    'username': u.username, 'full_name': '',
                                    'is_agent': False}
                    return None

                with mock.patch(
                    'tasks.services.workflow_service.WorkflowService._seat_satisfies_required_capability',
                    side_effect=capable,
                ), mock.patch(
                    'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
                    side_effect=self._recurrence_route_resolver,
                ), mock.patch(
                    'tasks.user_service.user_service.get_user_by_email',
                    side_effect=_by_email,
                ), mock.patch(
                    # main's _apply_resolved_assignee enforces the PLS-278
                    # seat-routability verdict (staffed ∧ capable ∧ live ∧
                    # under-capacity); the July-era stubs predate it.
                    'tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment',
                    return_value=True,
                ):
                    result = fire_recurring_due(item.pk)
                item.refresh_from_db()
                self.assertIn('Fired recurring', result)
                self.assertEqual(item.assignment_group, 'engineering')
                self.assertEqual(item.assignee_email, self.alt.email)
                self.assertEqual(
                    item.metadata['recurring_routing']['discrepancy']['reason'],
                    expected_reason,
                )

    @mock.patch('requests.post')
    @mock.patch(
        'tasks.services.workflow_service.WorkflowService._member_can_execute_from_status',
        return_value=True,
    )
    @mock.patch(
        'tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment',
        return_value=True,
    )
    def test_capability_incompatible_fallback_holds_without_consuming_boundary(
            self, _available, _can_execute, _post):
        TeamRoster.objects.create(
            team_name='capability-team', organization_slug='shizuha',
            organization_id=1, tiers=[[self.user.email]], is_active=True,
        )
        item = self._item(
            status='scheduled',
            assignment_group='capability-team',
            labels={'needs:kubeconfig': True},
            metadata={
                'recurring_owner_team': 'capability-team',
                'recurring_routing': {
                    'fallback_assignment_group': 'engineering',
                },
            },
        )
        original_due = item.due_date

        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._seat_satisfies_required_capability',
            return_value=False,
        ):
            first = fire_recurring_due(item.pk)
            second = fire_recurring_due(item.pk)
        item.refresh_from_db()
        self.assertIn('Held recurring', first)
        self.assertIn('Held recurring', second)
        self.assertEqual(item.status, 'scheduled')
        self.assertEqual(item.due_date, original_due)
        self.assertIsNone(item.assignee_id)
        self.assertEqual(
            item.metadata['recurring_cycle']['active_generation'],
            0,
        )
        self.assertEqual(
            Comment.objects.filter(
                item=item,
                content__contains='PLS-528 recurrence-routing alert',
            ).count(),
            1,
        )


class PLAT8821ParkedEscalationSuppressionTests(RecurringCycleStaleTests):
    """PLAT-8821: the PLS-214 missed-period escalation must never bounce the
    assignee of a parked ticket. CON-389 took 5 failover hops in one day
    against the operator's twice-stated park directive — the escalation path
    routed on assignment state alone. A recent operator/lead park disposition
    suppresses the bounce (suppression recorded, park stands)."""

    def test_escalation_suppressed_for_operator_park_disposition(self):
        item = self._item(metadata={'recurring_cycle': {'consecutive_skips': 2}})
        Comment.objects.create(
            item=item, author_id=0, author_email='operator@example.test',
            content=(
                'Do not run this ticket as a weekly GLM reminder — recurrence '
                'lives in beat_schedule. Park directive; missed-period '
                'failovers were the anti-pattern.'
            ),
        )
        with mock.patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee') as resolve, \
             mock.patch('tasks.tasks._notify_recurring_cycle_team_lead') as notify, \
             mock.patch('requests.post'):
            fire_recurring_due(item.pk)
        item.refresh_from_db()
        # Park stands: no assignee bounce, no lead notification.
        self.assertEqual(item.assignee_email, 'rin@shizuha.com')
        resolve.assert_not_called()
        notify.assert_not_called()
        # Suppression is recorded and the crossing is consumed (no re-fire at 6).
        self.assertEqual(item.metadata['recurring_cycle']['escalated_at_skips'], 3)
        self.assertTrue(Comment.objects.filter(
            item=item, content__icontains='Suppressed: item is parked').exists())
        self.assertTrue(Comment.objects.filter(
            item=item, content__icontains='PLS-214/PLAT-8821').exists())

    def test_escalation_suppression_does_not_re_fire_next_crossing(self):
        item = self._item(metadata={'recurring_cycle': {'consecutive_skips': 2}})
        Comment.objects.create(
            item=item, author_id=0, author_email='operator@example.test',
            content='[no-failover] parked by operator directive.',
        )
        with mock.patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee') as resolve, \
             mock.patch('requests.post'):
            fire_recurring_due(item.pk)
        item.refresh_from_db()
        self.assertEqual(item.metadata['recurring_cycle']['escalated_at_skips'], 3)

        item.due_date = timezone.now() - timedelta(minutes=1)
        item.save(update_fields=['due_date'])
        with mock.patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee') as resolve2, \
             mock.patch('requests.post') as post2:
            fire_recurring_due(item.pk)
        item.refresh_from_db()
        resolve2.assert_not_called()
        post2.assert_not_called()
