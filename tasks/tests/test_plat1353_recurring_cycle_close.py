from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from tasks.models import Comment, Item, ItemLink, Status, Workflow, WorkflowStatus
from tasks.services.recurring_cycle_close import (
    alert_recurring_cycle_auto_close_failure,
    close_recurring_cycle_if_complete,
)


class RecurringCycleAutoCloseTests(TestCase):
    def setUp(self):
        Status.objects.get_or_create(
            slug='scheduled',
            defaults={
                'name': 'Scheduled',
                'category': 'scheduled',
                'color': '#64748b',
                'position': 99,
            },
        )

    def _recurring_item(self, **kw):
        kw.setdefault('title', 'weekly durability audit')
        kw.setdefault('mode', 'task')
        kw.setdefault('status', 'open')
        kw.setdefault('workflow_name', 'autonomous-dev')
        kw.setdefault('workflow_status', 'Open')
        kw.setdefault('is_recurring', True)
        kw.setdefault('frequency', 'weekly')
        kw.setdefault('due_date', timezone.now() - timedelta(hours=1))
        return Item.objects.create(**kw)

    def test_merged_pr_with_cycle_completion_evidence_parks_current_cycle(self):
        item = self._recurring_item(metadata={
            'pull_requests': [{
                'url': 'https://github.com/shizuha-labs/deploy/pull/280',
                'repo': 'shizuha-labs/deploy',
                'number': 280,
                'status': 'merged',
            }]
        })
        old_due = item.due_date
        Comment.objects.create(
            item=item,
            author_id=1,
            author_email='kai@shizuha.com',
            content='cycle 2026-07-04 complete — deliverable merged and verified',
        )

        result = close_recurring_cycle_if_complete(
            item,
            trigger_kind='PR',
            trigger_label='https://github.com/shizuha-labs/deploy/pull/280',
            actor_email='github-webhook@shizuha.local',
        )

        self.assertTrue(result.closed)
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')
        self.assertGreater(item.due_date, old_due)
        self.assertFalse(item.overdue_notified)
        self.assertFalse(item.reminder_sent)
        cycle = item.metadata['recurring_cycle']
        self.assertEqual(cycle['consecutive_skips'], 0)
        self.assertEqual(cycle['last_auto_closed_trigger'], 'PR:https://github.com/shizuha-labs/deploy/pull/280')
        self.assertTrue(Comment.objects.filter(item=item, content__contains='[recurring-cycle auto-close]').exists())

        duplicate = close_recurring_cycle_if_complete(
            item,
            trigger_kind='PR',
            trigger_label='https://github.com/shizuha-labs/deploy/pull/280',
            actor_email='github-webhook@shizuha.local',
        )
        self.assertFalse(duplicate.closed)
        self.assertEqual(
            Comment.objects.filter(item=item, content__contains='[recurring-cycle auto-close]').count(),
            1,
        )

    def test_merged_pr_without_explicit_cycle_evidence_does_not_close(self):
        item = self._recurring_item()

        result = close_recurring_cycle_if_complete(
            item,
            trigger_kind='PR',
            trigger_label='https://github.com/shizuha-labs/deploy/pull/281',
            actor_email='github-webhook@shizuha.local',
        )

        self.assertFalse(result.closed)
        self.assertEqual(result.reason, 'missing_completion_evidence')
        item.refresh_from_db()
        self.assertEqual(item.status, 'open')

    def test_terminal_linked_deliverable_task_closes_parent_cycle(self):
        future_due = timezone.now() + timedelta(days=5)
        parent = self._recurring_item(
            due_date=future_due,
            metadata={'recurring_cycle': {
                'cycle_revision': 3,
                'active_generation': 1,
                'completed_generation': 0,
                'consecutive_skips': 3,
            }},
        )
        child = Item.objects.create(
            title='deliverable PR/task',
            mode='task',
            status='in_progress',
            workflow_name='autonomous-dev',
            workflow_status='In Progress',
        )
        ItemLink.objects.create(
            source_item=parent,
            target_item=child,
            link_type='relates_to',
            created_by_id=1,
            created_by_email='kai@shizuha.com',
        )
        Comment.objects.create(
            item=parent,
            author_id=1,
            author_email='kai@shizuha.com',
            content='[recurring-cycle complete] current cycle deliverable is complete',
        )

        child.status = 'done'
        child.workflow_status = 'Done'
        child._activity_actor = (1, 'kai@shizuha.com')
        child.save(update_fields=['status', 'workflow_status'])

        parent.refresh_from_db()
        self.assertEqual(parent.status, 'scheduled')
        self.assertEqual(parent.due_date, future_due)
        self.assertEqual(parent.metadata['recurring_cycle']['cycle_revision'], 4)
        self.assertEqual(parent.metadata['recurring_cycle']['completed_generation'], 1)
        self.assertEqual(parent.metadata['recurring_cycle']['consecutive_skips'], 0)
        self.assertTrue(Comment.objects.filter(item=parent, content__contains='linked task').exists())

    def test_workflow_missing_scheduled_gap_state_gets_backfilled_before_parking(self):
        open_status, _ = Status.objects.get_or_create(
            slug='open',
            defaults={'name': 'Open', 'category': 'todo', 'color': '#2563eb', 'position': 1},
        )
        workflow = Workflow.objects.create(
            name='PLAT1353 No Scheduled Workflow',
            slug='plat1353-no-scheduled',
            description='Regression workflow without scheduled state',
            initial_status=open_status,
        )
        WorkflowStatus.objects.create(workflow=workflow, status=open_status, position=1)
        WorkflowStatus.objects.filter(workflow=workflow, status__slug='scheduled').delete()
        item = self._recurring_item(
            workflow_name=workflow.slug,
            status='open',
            workflow_status='Open',
        )
        Comment.objects.create(
            item=item,
            author_id=1,
            author_email='kai@shizuha.com',
            content='current cycle complete — deliverable merged',
        )

        result = close_recurring_cycle_if_complete(
            item,
            trigger_kind='PR',
            trigger_label='https://github.com/shizuha-labs/deploy/pull/282',
            actor_email='github-webhook@shizuha.local',
        )

        self.assertTrue(result.closed)
        self.assertTrue(WorkflowStatus.objects.filter(workflow=workflow, status__slug='scheduled').exists())
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')
        self.assertEqual(item.workflow_status, 'Scheduled')

    def test_terminal_child_linked_from_child_side_closes_parent_cycle(self):
        parent = self._recurring_item()
        child = Item.objects.create(
            title='deliverable PR/task',
            mode='task',
            status='in_progress',
            workflow_name='autonomous-dev',
            workflow_status='In Progress',
        )
        ItemLink.objects.create(
            source_item=child,
            target_item=parent,
            link_type='relates_to',
            created_by_id=1,
            created_by_email='kai@shizuha.com',
        )
        Comment.objects.create(
            item=parent,
            author_id=1,
            author_email='kai@shizuha.com',
            content='[recurring-cycle complete] current cycle deliverable is complete',
        )

        child.status = 'done'
        child.workflow_status = 'Done'
        child._activity_actor = (1, 'kai@shizuha.com')
        child.save(update_fields=['status', 'workflow_status'])

        parent.refresh_from_db()
        self.assertEqual(parent.status, 'scheduled')
        self.assertTrue(Comment.objects.filter(item=parent, content__contains='linked task').exists())

    @patch('tasks.services.notifications.UserNotificationService.notify_user')
    @patch('tasks.user_service.user_service.get_user_by_email')
    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_lead_email')
    def test_failure_path_records_andon_liveness_and_notifies_owner_lead(self, resolve_lead, get_user_by_email, notify_user):
        item = self._recurring_item()
        item.assignment_group = 'engineering'
        resolve_lead.return_value = 'lead@shizuha.com'
        get_user_by_email.return_value = {'id': 42, 'email': 'lead@shizuha.com'}

        notified = alert_recurring_cycle_auto_close_failure(
            item,
            context='unit_test_hook',
            exc=RuntimeError('boom'),
        )

        self.assertTrue(notified)
        notify_user.assert_called_once()
        args, kwargs = notify_user.call_args
        self.assertEqual(args[0], 42)
        self.assertEqual(kwargs['event_type'], 'alert')
        self.assertEqual(kwargs['severity'], 'error')
        self.assertTrue(Comment.objects.filter(item=item, content__contains='🔴 ANDON [PLAT-1353').exists())
        self.assertIsNotNone(cache.get('pulse:plat1353_recurring_cycle_close:last_failure'))

    def test_prior_cycle_deliverable_does_not_close_later_period_nor_reset_escalation(self):
        """PLS-855 regression: a deliverable DIRECTED by a prior period cannot close
        the CURRENT period's cycle nor reset its missed-period escalation.

        Mirrors PLAT-1189: an 8h/standing recurrence had accumulated 9 consecutive
        misses (PLS-214 escalation active) when a stale directive — created in an
        earlier generation — reached terminal inside the current window. The old
        behavior parked the cycle and zeroed the escalation even though the period
        never ran.

        This fixture spans two recurrence periods and runs the REAL caller
        sequence (verify-before-push, stateful-recurrence clause): each linked
        child's status change fires the post_save terminal-child signal, which
        drives `close_recurring_parents_for_terminal_child` ->
        `close_recurring_cycle_if_complete` with the child's creation time as the
        generation bound. The unit of regression is that caller sequence, not a
        direct helper call.
        """
        now = timezone.now()
        # The current generation's period window: it opened when the timer
        # consumed `period_opened` and is due at `now`.
        period_opened = now - timedelta(days=1)
        parent = self._recurring_item(
            due_date=now,
            metadata={'recurring_cycle': {
                'cycle_revision': 7,
                'active_generation': 4,
                'completed_generation': 3,
                'consumed_boundary': period_opened.isoformat(),
                'next_boundary': now.isoformat(),
                'consecutive_skips': 9,
                'escalated_at_skips': 9,
            }},
        )
        Comment.objects.create(
            item=parent,
            author_id=1,
            author_email='kai@shizuha.com',
            content='[recurring-cycle complete] deliverable directed by the standing cycle',
        )

        def _link_and_terminalize(title, directed_at):
            child = Item.objects.create(
                title=title,
                mode='task',
                status='in_progress',
                workflow_name='autonomous-dev',
                workflow_status='In Progress',
            )
            # Backdate creation to the period that DIRECTED the deliverable.
            child.created_at = directed_at
            child.save(update_fields=['created_at', 'updated_at'])
            ItemLink.objects.create(
                source_item=parent,
                target_item=child,
                link_type='relates_to',
                created_by_id=1,
                created_by_email='kai@shizuha.com',
            )
            child.status = 'done'
            child.workflow_status = 'Done'
            child._activity_actor = (1, 'kai@shizuha.com')
            child.save(update_fields=['status', 'workflow_status'])
            return child

        # --- Period-N deliverable directed BEFORE the current window ---
        _link_and_terminalize(
            'prior-period executive directive (VEN-218 shape)',
            directed_at=period_opened - timedelta(hours=1),
        )

        parent.refresh_from_db()
        cycle = parent.metadata['recurring_cycle']
        # Prior-gen evidence is a no-op: status stays ACTIVE (not parked),
        # escalation counters untouched, nothing advanced, no auto-close comment.
        self.assertEqual(parent.status, 'open')
        self.assertEqual(cycle['consecutive_skips'], 9)
        self.assertEqual(cycle['escalated_at_skips'], 9)
        self.assertEqual(cycle['completed_generation'], 3)
        self.assertFalse(
            Comment.objects.filter(item=parent, content__contains='[recurring-cycle auto-close]').exists()
        )

        # --- Current-period deliverable directed INSIDE the window ---
        _link_and_terminalize(
            'current-period deliverable',
            directed_at=now,
        )

        parent.refresh_from_db()
        cycle = parent.metadata['recurring_cycle']
        # Genuine current-gen completion parks the cycle and resets escalation.
        self.assertEqual(parent.status, 'scheduled')
        self.assertGreater(parent.due_date, now)
        self.assertEqual(cycle['consecutive_skips'], 0)
        self.assertEqual(cycle['escalated_at_skips'], 0)
        self.assertEqual(cycle['completed_generation'], 4)
        self.assertTrue(
            Comment.objects.filter(item=parent, content__contains='[recurring-cycle auto-close]').exists()
        )

    def test_prior_merged_pr_does_not_close_current_cycle(self):
        """PLS-855 PR-path: a merged PR whose merge date predates the current
        generation's window must not park the current cycle."""
        now = timezone.now()
        period_opened = now - timedelta(days=1)
        item = self._recurring_item(
            due_date=now,
            metadata={
                'recurring_cycle': {
                    'cycle_revision': 4,
                    'active_generation': 3,
                    'completed_generation': 2,
                    'consumed_boundary': period_opened.isoformat(),
                    'next_boundary': now.isoformat(),
                    'consecutive_skips': 3,
                    'escalated_at_skips': 3,
                },
            },
        )
        Comment.objects.create(
            item=item,
            author_id=1,
            author_email='kai@shizuha.com',
            content='[recurring-cycle complete] deliverable merged and verified',
        )

        result = close_recurring_cycle_if_complete(
            item,
            trigger_kind='PR',
            trigger_label='https://github.com/shizuha-labs/deploy/pull/283',
            actor_email='github-webhook@shizuha.local',
            trigger_at=period_opened - timedelta(hours=2),
        )

        self.assertFalse(result.closed)
        self.assertEqual(result.reason, 'stale_evidence_prior_generation')
        item.refresh_from_db()
        self.assertEqual(item.status, 'open')
        cycle = item.metadata['recurring_cycle']
        self.assertEqual(cycle['consecutive_skips'], 3)
        self.assertEqual(cycle['escalated_at_skips'], 3)
