"""
Tests for Shizuha Hub models.
Tests Item model (unified tasks, actions, alerts) and all rule models.
"""
import uuid

from django.test import TestCase
from django.utils import timezone
from django.db import IntegrityError, transaction
from datetime import timedelta
from decimal import Decimal

from tasks.models import (
    Item, Task,  # Task is alias for Item
    ApprovalRule, SilenceRule, EscalationRule, InhibitionRule,
    Subscription, FingerprintConfig, ItemStateEventOutbox,
)
from .base import create_blocked_item, satisfy_blocked_invariant


class ItemModelBasicTests(TestCase):
    """Test basic Item model functionality."""

    def test_create_task_mode(self):
        """Test creating an item in task mode."""
        item = Item.objects.create(
            mode='task',
            title='Test Task',
            description='A test task',
            priority='high'
        )
        self.assertEqual(item.mode, 'task')
        self.assertEqual(item.title, 'Test Task')
        # PLS-230: task-mode items auto-attach the Simple workflow, which coerces
        # the legacy default 'pending' to the canonical 'todo'.
        self.assertEqual(item.status, 'todo')
        self.assertEqual(item.item_type, 'task.generic')

    def test_create_action_mode(self):
        """Test creating an item in action mode."""
        item = Item.objects.create(
            mode='action',
            title='Test Action',
            item_type='action.hedging.short',
            requires_approval=True,
            payload={'symbol': 'BTCUSDT', 'quantity': 0.1}
        )
        self.assertEqual(item.mode, 'action')
        self.assertTrue(item.requires_approval)
        self.assertEqual(item.payload['symbol'], 'BTCUSDT')

    def test_create_alert_mode(self):
        """Test creating an item in alert mode."""
        item = Item.objects.create(
            mode='alert',
            title='High CPU Alert',
            item_type='alert.error',
            severity='critical',
            source='prometheus',
            labels={'host': 'web-01', 'service': 'nginx'}
        )
        self.assertEqual(item.mode, 'alert')
        self.assertEqual(item.severity, 'critical')
        self.assertEqual(item.labels['host'], 'web-01')
        self.assertEqual(item.status, 'pending')

    def test_task_alias_works(self):
        """Test that Task alias works correctly."""
        task = Task.objects.create(
            title='Task via alias',
            mode='task'
        )
        self.assertIsInstance(task, Item)
        self.assertEqual(Task.objects.count(), 1)
        self.assertEqual(Item.objects.count(), 1)

    def test_str_representation(self):
        """Test string representation."""
        item = Item.objects.create(title='My Item', item_type='task.generic')
        self.assertEqual(str(item), '[task] My Item (task.generic)')


class ItemStateGenerationTests(TestCase):
    """PLS-467 producer-order/cardinality regression coverage."""

    def test_model_save_coalesces_tracked_delta_into_one_generation(self):
        item = Item.objects.create(title='generation typed path')

        item.priority = 'high'
        item.status = 'in_progress'
        item.save(update_fields=['priority', 'status', 'updated_at'])

        item.refresh_from_db()
        self.assertEqual(item.state_generation, 1)
        event = ItemStateEventOutbox.objects.get(
            item=item, state_generation=1
        )
        self.assertEqual(event.writer_kind, 'typed')
        self.assertEqual(event.changed_kinds, ['priority', 'status'])
        self.assertEqual(event.envelope['data']['state_generation'], '1')

    def test_bulk_writer_gets_one_generic_generation_and_event(self):
        item = Item.objects.create(title='generation fallback')

        Item.objects.filter(pk=item.pk).update(priority='urgent')

        item.refresh_from_db()
        self.assertEqual(item.state_generation, 1)
        event = ItemStateEventOutbox.objects.get(
            item=item, state_generation=1
        )
        self.assertEqual(event.writer_kind, 'unattributed_writer')
        self.assertEqual(event.changed_kinds, ['priority'])

    def test_rollback_leaves_generation_and_outbox_unchanged(self):
        item = Item.objects.create(title='generation rollback')

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                item.priority = 'high'
                item.save(update_fields=['priority', 'updated_at'])
                raise RuntimeError('force rollback after producer')

        item.refresh_from_db()
        self.assertEqual(item.state_generation, 0)
        self.assertFalse(ItemStateEventOutbox.objects.filter(item=item).exists())

    def test_display_only_write_does_not_advance_state_generation(self):
        item = Item.objects.create(title='generation display')

        item.description = 'context only'
        item.save(update_fields=['description', 'updated_at'])

        item.refresh_from_db()
        self.assertEqual(item.state_generation, 0)
        self.assertFalse(ItemStateEventOutbox.objects.filter(item=item).exists())

    def test_stale_orm_instance_cannot_rewind_bulk_advanced_generation(self):
        item = Item.objects.create(title='generation stale instance')
        Item.objects.filter(pk=item.pk).update(priority='urgent')

        # ``item`` still carries generation 0.  A later full save of unrelated
        # display state must preserve the trigger-allocated generation 1.
        item.description = 'context after bulk writer'
        item.save()

        item.refresh_from_db()
        self.assertEqual(item.state_generation, 1)
        self.assertEqual(item.priority, 'urgent')
        self.assertEqual(
            ItemStateEventOutbox.objects.filter(item=item).count(), 1
        )

    def test_pls1024_first_write_succeeds(self):
        """PLS-1024: the first typed outbox append for a generation commits."""
        item = Item.objects.create(title='pls1024 first write')

        item.priority = 'high'
        item.save(update_fields=['priority', 'updated_at'])

        item.refresh_from_db()
        self.assertEqual(item.state_generation, 1)
        event = ItemStateEventOutbox.objects.get(
            item=item, state_generation=1
        )
        self.assertEqual(event.writer_kind, 'typed')
        self.assertEqual(event.changed_kinds, ['priority'])
        self.assertEqual(event.envelope['data']['state_generation'], '1')

    def test_pls1024_retry_after_unique_violation_adopts_existing_row(self):
        """PLS-1024: IntegrityError on uniq_item_state_event_generation adopts
        the leftover row and continues instead of 500ing the retry.
        """
        item = Item.objects.create(title='pls1024 unique-violation retry')
        stale_token = uuid.uuid4()
        leftover = ItemStateEventOutbox.objects.create(
            id=stale_token,
            item=item,
            mutation_token=stale_token,
            state_generation=1,
            changed_kinds=['status'],
            envelope={'specversion': '1.0', 'id': str(stale_token)},
            writer_kind='unattributed_writer',
        )

        item.priority = 'high'
        item.status = 'in_progress'
        item.save(update_fields=['priority', 'status', 'updated_at'])

        item.refresh_from_db()
        self.assertEqual(item.state_generation, 1)
        events = list(ItemStateEventOutbox.objects.filter(
            item=item, state_generation=1,
        ))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.id, leftover.id)
        self.assertEqual(event.writer_kind, 'typed')
        self.assertNotEqual(event.mutation_token, stale_token)
        self.assertEqual(event.changed_kinds, ['priority', 'status'])
        self.assertEqual(event.envelope['data']['state_generation'], '1')

    def test_pls1024_resume_after_partial_event(self):
        """PLS-1024: blocker-resume of the same generation must not 500 when a
        partial leftover outbox row already occupies (item, next generation).
        Mirrors WIKI-66 / item 28329: outbox ahead of tasks_task.state_generation.
        """
        item = create_blocked_item(
            title='pls1024 resume leftover',
            pre_block_status='in_progress',
        )
        item.refresh_from_db()
        current_gen = item.state_generation
        stale_token = uuid.uuid4()
        leftover = ItemStateEventOutbox.objects.create(
            id=stale_token,
            item=item,
            mutation_token=stale_token,
            state_generation=current_gen + 1,
            changed_kinds=['status'],
            envelope={'specversion': '1.0', 'id': str(stale_token)},
            writer_kind='typed',
        )

        item.status = 'in_progress'
        item.save(update_fields=['status', 'updated_at'])

        item.refresh_from_db()
        self.assertEqual(item.state_generation, current_gen + 1)
        self.assertEqual(item.status, 'in_progress')
        events = list(ItemStateEventOutbox.objects.filter(
            item=item, state_generation=current_gen + 1,
        ))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, leftover.id)
        self.assertEqual(events[0].writer_kind, 'typed')
        self.assertNotEqual(events[0].mutation_token, stale_token)


class ItemModelStatusTests(TestCase):
    """Test Item model status transitions."""

    def test_default_status_is_todo(self):
        """PLS-230: a task-mode item's default status is coerced to 'todo' by the
        auto-attached Simple workflow (legacy default was 'pending')."""
        item = Item.objects.create(title='Test')
        self.assertEqual(item.status, 'todo')

    def test_task_statuses_coerce_to_simple_workflow(self):
        """PLS-230: legacy task statuses are healed to the canonical Simple-workflow
        slugs on save (_coerce_invalid_status). Statuses already valid in the
        workflow are unchanged; legacy ones remap deterministically."""
        expected = {
            'pending': 'todo',        # legacy todo-category -> workflow's todo
            'in_progress': 'in_progress',  # already valid
            'completed': 'done',      # legacy done-category -> workflow's done
            'cancelled': 'done',      # no 'cancelled' in Simple -> done category
            'blocked': 'blocked',     # already valid
        }
        for legacy, canonical in expected.items():
            factory = create_blocked_item if canonical == 'blocked' else Item.objects.create
            item = factory(title=f'Task {legacy}', status=legacy)
            self.assertEqual(item.status, canonical, f'{legacy} should coerce to {canonical}')

    def test_action_statuses(self):
        """Test action-specific statuses."""
        for status in ['pending', 'approved', 'rejected', 'executing', 'completed', 'failed']:
            item = Item.objects.create(
                title=f'Action {status}',
                mode='action',
                status=status
            )
            self.assertEqual(item.status, status)

    def test_alert_statuses(self):
        """Test alert-specific statuses."""
        for status in ['firing', 'acknowledged', 'resolved', 'silenced']:
            item = Item.objects.create(
                title=f'Alert {status}',
                mode='alert',
                status=status
            )
            self.assertEqual(item.status, status)


class ItemModelMethodTests(TestCase):
    """Test Item model methods."""

    def test_is_overdue_with_past_due_date(self):
        """Test is_overdue returns True for past due dates."""
        item = Item.objects.create(
            title='Overdue Task',
            due_date=timezone.now() - timedelta(days=1),
            status='pending'
        )
        self.assertTrue(item.is_overdue())

    def test_is_overdue_with_future_due_date(self):
        """Test is_overdue returns False for future due dates."""
        item = Item.objects.create(
            title='Future Task',
            due_date=timezone.now() + timedelta(days=1),
            status='pending'
        )
        self.assertFalse(item.is_overdue())

    def test_is_overdue_completed_task_not_overdue(self):
        """Test completed tasks are not overdue."""
        item = Item.objects.create(
            title='Completed Task',
            due_date=timezone.now() - timedelta(days=1),
            status='completed'
        )
        self.assertFalse(item.is_overdue())

    def test_is_overdue_no_due_date(self):
        """Test items without due date are not overdue."""
        item = Item.objects.create(title='No Due Date')
        self.assertFalse(item.is_overdue())

    def test_days_until_due_positive(self):
        """Test days_until_due with future date."""
        item = Item.objects.create(
            title='Future Task',
            due_date=timezone.now() + timedelta(days=5)
        )
        days = item.days_until_due()
        self.assertIsNotNone(days)
        self.assertGreaterEqual(days, 4)  # Could be 4-5 depending on time

    def test_days_until_due_negative(self):
        """Test days_until_due with past date."""
        item = Item.objects.create(
            title='Past Task',
            due_date=timezone.now() - timedelta(days=3)
        )
        days = item.days_until_due()
        self.assertIsNotNone(days)
        self.assertLessEqual(days, -2)  # Could be -2 to -3

    def test_days_until_due_no_due_date(self):
        """Test days_until_due returns None without due date."""
        item = Item.objects.create(title='No Due Date')
        self.assertIsNone(item.days_until_due())

    def test_is_active_pending_task(self):
        """Test is_active for pending task."""
        item = Item.objects.create(title='Pending', status='pending')
        self.assertTrue(item.is_active())

    def test_is_active_completed_task(self):
        """Test is_active for completed task."""
        item = Item.objects.create(title='Completed', status='completed')
        self.assertFalse(item.is_active())

    def test_is_active_firing_alert(self):
        """Test is_active for firing alert."""
        item = Item.objects.create(
            title='Firing Alert',
            mode='alert',
            status='firing'
        )
        self.assertTrue(item.is_active())

    def test_is_active_resolved_alert(self):
        """Test is_active for resolved alert."""
        item = Item.objects.create(
            title='Resolved Alert',
            mode='alert',
            status='resolved'
        )
        self.assertFalse(item.is_active())

    def test_is_silenced_no_silencing(self):
        """Test is_silenced returns False when not silenced."""
        item = Item.objects.create(title='Not Silenced')
        self.assertFalse(item.is_silenced())

    def test_is_silenced_with_future_until(self):
        """Test is_silenced returns True when silenced_until is in future."""
        item = Item.objects.create(
            title='Silenced',
            silenced_until=timezone.now() + timedelta(hours=1)
        )
        self.assertTrue(item.is_silenced())

    def test_is_silenced_with_past_until(self):
        """Test is_silenced returns False when silenced_until is in past."""
        item = Item.objects.create(
            title='Was Silenced',
            silenced_until=timezone.now() - timedelta(hours=1)
        )
        self.assertFalse(item.is_silenced())

    def test_get_tags_list_empty(self):
        """Test get_tags_list with no tags."""
        item = Item.objects.create(title='No Tags')
        self.assertEqual(item.get_tags_list(), [])

    def test_get_tags_list_with_tags(self):
        """Test get_tags_list with comma-separated tags."""
        item = Item.objects.create(title='Tagged', tags='foo, bar, baz')
        self.assertEqual(item.get_tags_list(), ['foo', 'bar', 'baz'])

    def test_get_tags_list_with_whitespace(self):
        """Test get_tags_list handles whitespace."""
        item = Item.objects.create(title='Tagged', tags='  foo  ,  bar  ')
        self.assertEqual(item.get_tags_list(), ['foo', 'bar'])


class ItemApprovalTests(TestCase):
    """Test Item approval/rejection methods."""

    def test_approve_action(self):
        """Test approving an action."""
        action = Item.objects.create(
            mode='action',
            title='Action to Approve',
            requires_approval=True,
            status='pending'
        )
        action.approve(approved_by='user:admin')

        self.assertEqual(action.status, 'approved')
        self.assertEqual(action.approved_by, 'user:admin')
        self.assertIsNotNone(action.approved_at)

    def test_reject_action(self):
        """Test rejecting an action."""
        action = Item.objects.create(
            mode='action',
            title='Action to Reject',
            requires_approval=True,
            status='pending'
        )
        action.reject(reason='Not allowed', rejected_by='user:admin')

        self.assertEqual(action.status, 'rejected')
        self.assertEqual(action.rejection_reason, 'Not allowed')

    def test_approve_already_approved_is_idempotent(self):
        """Test approving already approved action is idempotent."""
        action = Item.objects.create(
            mode='action',
            title='Already Approved',
            status='approved'
        )
        # Current implementation allows re-approving (idempotent)
        action.approve(approved_by='user:admin')
        self.assertEqual(action.status, 'approved')

    def test_reject_completed_action_still_rejects(self):
        """Test rejecting completed action changes status to rejected."""
        action = Item.objects.create(
            mode='action',
            title='Completed',
            status='completed'
        )
        # Current implementation allows rejecting any action
        action.reject(reason='Too late')
        self.assertEqual(action.status, 'rejected')


class ItemAlertTests(TestCase):
    """Test Item alert-specific methods."""

    def test_acknowledge_alert(self):
        """Test acknowledging an alert."""
        alert = Item.objects.create(
            mode='alert',
            title='Alert to Ack',
            status='firing'
        )
        alert.acknowledge(acknowledged_by='user:oncall')

        self.assertEqual(alert.status, 'acknowledged')
        self.assertEqual(alert.acknowledged_by, 'user:oncall')
        self.assertIsNotNone(alert.acknowledged_at)

    def test_resolve_alert(self):
        """Test resolving an alert."""
        alert = Item.objects.create(
            mode='alert',
            title='Alert to Resolve',
            status='firing'
        )
        alert.resolve(resolved_by='user:oncall', note='Fixed the issue')

        self.assertEqual(alert.status, 'resolved')
        self.assertEqual(alert.resolved_by, 'user:oncall')
        self.assertEqual(alert.resolution_note, 'Fixed the issue')
        self.assertIsNotNone(alert.resolved_at)

    def test_silence_alert(self):
        """Test silencing an alert."""
        alert = Item.objects.create(
            mode='alert',
            title='Alert to Silence',
            status='firing'
        )
        silence_until = timezone.now() + timedelta(minutes=60)
        alert.silence(until=silence_until, silenced_by='user:admin', reason='Maintenance')

        self.assertTrue(alert.is_silenced())
        self.assertEqual(alert.silenced_by, 'user:admin')
        self.assertEqual(alert.silence_reason, 'Maintenance')
        # Check silenced_until is approximately 60 minutes from now
        self.assertAlmostEqual(
            alert.silenced_until.timestamp(),
            silence_until.timestamp(),
            delta=5  # Within 5 seconds
        )

    def test_acknowledge_resolved_alert_still_works(self):
        """Test acknowledging resolved alert changes its status back."""
        alert = Item.objects.create(
            mode='alert',
            title='Resolved',
            status='resolved'
        )
        # The model allows acknowledging even resolved alerts (sets to acknowledged)
        alert.acknowledge(acknowledged_by='user:admin')
        self.assertEqual(alert.status, 'acknowledged')
        self.assertEqual(alert.acknowledged_by, 'user:admin')


class ItemFingerprintTests(TestCase):
    """Test Item fingerprint computation."""

    def test_compute_fingerprint_basic(self):
        """Test basic fingerprint computation."""
        # compute_fingerprint is a static method that takes explicit params
        fingerprint = Item.compute_fingerprint(
            item_type='alert.error',
            source='prometheus',
            labels={'host': 'web-01'}
        )

        self.assertIsNotNone(fingerprint)
        self.assertEqual(len(fingerprint), 32)  # MD5 hex digest length

    def test_compute_fingerprint_deterministic(self):
        """Test fingerprint is deterministic for same data."""
        # Same source, item_type, and labels should give same fingerprint
        fp1 = Item.compute_fingerprint(
            item_type='alert.error',
            source='prometheus',
            labels={'host': 'web-01'}
        )
        fp2 = Item.compute_fingerprint(
            item_type='alert.error',
            source='prometheus',
            labels={'host': 'web-01'}
        )
        self.assertEqual(fp1, fp2)

    def test_compute_fingerprint_different_labels(self):
        """Test different labels produce different fingerprints."""
        fp1 = Item.compute_fingerprint(
            item_type='alert.error',
            source='prometheus',
            labels={'host': 'web-01'}
        )
        fp2 = Item.compute_fingerprint(
            item_type='alert.error',
            source='prometheus',
            labels={'host': 'web-02'}  # Different host
        )
        self.assertNotEqual(fp1, fp2)


class ItemUniqueConstraintTests(TestCase):
    """Test unique constraint for active alert fingerprints."""

    def test_unique_fingerprint_for_firing_alerts(self):
        """Test that firing alerts with same fingerprint violate unique constraint."""
        fingerprint = 'test_fingerprint_12345'

        Item.objects.create(
            mode='alert',
            title='Alert 1',
            status='firing',
            fingerprint=fingerprint
        )

        # Should raise IntegrityError for duplicate fingerprint on active alert
        with self.assertRaises(IntegrityError), transaction.atomic():
            Item.objects.create(
                mode='alert',
                title='Alert 2',
                status='firing',
                fingerprint=fingerprint
            )

    def test_resolved_alert_allows_same_fingerprint(self):
        """Test that resolved alerts don't block same fingerprint."""
        fingerprint = 'test_fingerprint_resolved'

        # Create and resolve first alert
        alert1 = Item.objects.create(
            mode='alert',
            title='Alert 1',
            status='resolved',
            fingerprint=fingerprint
        )

        # Should be able to create new firing alert with same fingerprint
        alert2 = Item.objects.create(
            mode='alert',
            title='Alert 2',
            status='firing',
            fingerprint=fingerprint
        )

        self.assertEqual(alert2.status, 'firing')

    def test_tasks_not_affected_by_constraint(self):
        """Test that tasks are not affected by fingerprint constraint."""
        fingerprint = 'task_fingerprint'

        # Create multiple tasks with same fingerprint (should be allowed)
        Item.objects.create(
            mode='task',
            title='Task 1',
            fingerprint=fingerprint
        )
        Item.objects.create(
            mode='task',
            title='Task 2',
            fingerprint=fingerprint
        )

        self.assertEqual(Item.objects.filter(fingerprint=fingerprint).count(), 2)


class ApprovalRuleModelTests(TestCase):
    """Test ApprovalRule model."""

    def test_create_approval_rule(self):
        """Test creating an approval rule."""
        rule = ApprovalRule.objects.create(
            name='Auto-approve low priority',
            item_types=['action.hedging.*'],
            auto_approve=True,
            priority=10
        )
        self.assertEqual(rule.name, 'Auto-approve low priority')
        self.assertTrue(rule.auto_approve)
        self.assertTrue(rule.is_active)

    def test_approval_rule_ordering(self):
        """Test approval rules are ordered by priority."""
        ApprovalRule.objects.create(name='Low', priority=1)
        ApprovalRule.objects.create(name='High', priority=100)
        ApprovalRule.objects.create(name='Medium', priority=50)

        rules = list(ApprovalRule.objects.all())
        self.assertEqual(rules[0].name, 'High')
        self.assertEqual(rules[1].name, 'Medium')
        self.assertEqual(rules[2].name, 'Low')

    def test_approval_rule_str(self):
        """Test string representation."""
        rule = ApprovalRule.objects.create(name='Test Rule')
        self.assertEqual(str(rule), 'Test Rule (manual)')


class SilenceRuleModelTests(TestCase):
    """Test SilenceRule model."""

    def test_create_silence_rule(self):
        """Test creating a silence rule."""
        rule = SilenceRule.objects.create(
            name='Maintenance Silence',
            modes=['alert'],
            sources=['prometheus'],
            severities=['info', 'warning'],
            reason='Scheduled maintenance'
        )
        self.assertEqual(rule.name, 'Maintenance Silence')
        self.assertIn('alert', rule.modes)
        self.assertTrue(rule.is_active)

    def test_silence_rule_with_time_bounds(self):
        """Test silence rule with time bounds."""
        start = timezone.now()
        end = start + timedelta(hours=2)

        rule = SilenceRule.objects.create(
            name='Timed Silence',
            starts_at=start,
            ends_at=end
        )

        self.assertEqual(rule.starts_at, start)
        self.assertEqual(rule.ends_at, end)

    def test_is_currently_active_no_bounds(self):
        """Test is_currently_active with no time bounds."""
        rule = SilenceRule.objects.create(
            name='No Bounds',
            is_active=True
        )
        self.assertTrue(rule.is_currently_active())

    def test_is_currently_active_within_bounds(self):
        """Test is_currently_active within time bounds."""
        rule = SilenceRule.objects.create(
            name='Within Bounds',
            is_active=True,
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=1)
        )
        self.assertTrue(rule.is_currently_active())

    def test_is_currently_active_before_start(self):
        """Test is_currently_active before start time."""
        rule = SilenceRule.objects.create(
            name='Before Start',
            is_active=True,
            starts_at=timezone.now() + timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=2)
        )
        self.assertFalse(rule.is_currently_active())

    def test_is_currently_active_after_end(self):
        """Test is_currently_active after end time."""
        rule = SilenceRule.objects.create(
            name='After End',
            is_active=True,
            starts_at=timezone.now() - timedelta(hours=2),
            ends_at=timezone.now() - timedelta(hours=1)
        )
        self.assertFalse(rule.is_currently_active())

    def test_is_currently_active_when_inactive(self):
        """Test is_currently_active when rule is inactive."""
        rule = SilenceRule.objects.create(
            name='Inactive',
            is_active=False
        )
        self.assertFalse(rule.is_currently_active())

    def test_silence_rule_label_matchers(self):
        """Test silence rule with label matchers."""
        rule = SilenceRule.objects.create(
            name='Label Matcher',
            label_matchers={
                'host': 'web-*',
                'env': 'production'
            }
        )
        self.assertEqual(rule.label_matchers['host'], 'web-*')
        self.assertEqual(rule.label_matchers['env'], 'production')


class EscalationRuleModelTests(TestCase):
    """Test EscalationRule model."""

    def test_create_escalation_rule(self):
        """Test creating an escalation rule."""
        rule = EscalationRule.objects.create(
            name='Critical Alert Escalation',
            modes=['alert'],
            min_severity='warning',
            stages=[
                {
                    'delay_minutes': 5,
                    'actions': [
                        {'type': 'bump_severity', 'to': 'error'}
                    ]
                },
                {
                    'delay_minutes': 15,
                    'actions': [
                        {'type': 'bump_severity', 'to': 'critical'},
                        {'type': 'add_channels', 'channels': ['phone_call']}
                    ]
                }
            ]
        )

        self.assertEqual(rule.name, 'Critical Alert Escalation')
        self.assertEqual(len(rule.stages), 2)
        self.assertEqual(rule.stages[0]['delay_minutes'], 5)

    def test_escalation_rule_str(self):
        """Test string representation."""
        rule = EscalationRule.objects.create(name='Test Escalation')
        self.assertEqual(str(rule), 'Test Escalation (0 stages)')


class InhibitionRuleModelTests(TestCase):
    """Test InhibitionRule model."""

    def test_create_inhibition_rule(self):
        """Test creating an inhibition rule."""
        rule = InhibitionRule.objects.create(
            name='Database Cascade Inhibition',
            source_item_types=['alert.database.*'],
            source_severities=['critical'],
            target_item_types=['alert.api.*'],
            equal_labels=['env', 'cluster']
        )

        self.assertEqual(rule.name, 'Database Cascade Inhibition')
        self.assertIn('critical', rule.source_severities)
        self.assertIn('env', rule.equal_labels)

    def test_inhibition_rule_label_matchers(self):
        """Test inhibition rule with label matchers."""
        rule = InhibitionRule.objects.create(
            name='Label Matcher',
            source_label_matchers={'severity': 'critical'},
            target_label_matchers={'downstream': 'true'}
        )

        self.assertEqual(rule.source_label_matchers['severity'], 'critical')
        self.assertEqual(rule.target_label_matchers['downstream'], 'true')


class SubscriptionModelTests(TestCase):
    """Test Subscription model."""

    def test_create_webhook_subscription(self):
        """Test creating a webhook subscription."""
        sub = Subscription.objects.create(
            name='Alert Webhook',
            modes=['alert'],
            severities=['critical', 'error'],
            notify_type='webhook',
            webhook_url='https://example.com/webhook'
        )

        self.assertEqual(sub.notify_type, 'webhook')
        self.assertEqual(sub.webhook_url, 'https://example.com/webhook')

    def test_create_shizuha_notify_subscription(self):
        """Test creating a shizuha-notify subscription."""
        sub = Subscription.objects.create(
            name='Telegram Alerts',
            modes=['alert', 'action'],
            notify_type='shizuha_notify',
            notify_channels=['telegram']
        )

        self.assertEqual(sub.notify_type, 'shizuha_notify')
        self.assertIn('telegram', sub.notify_channels)


class FingerprintConfigModelTests(TestCase):
    """Test FingerprintConfig model."""

    def test_create_fingerprint_config(self):
        """Test creating a fingerprint config."""
        config = FingerprintConfig.objects.create(
            source='prometheus',
            label_fields=['alertname', 'instance', 'job'],
            payload_fields=['summary'],
            description='Prometheus alert deduplication config'
        )

        self.assertEqual(config.source, 'prometheus')
        self.assertIn('alertname', config.label_fields)
        self.assertIn('summary', config.payload_fields)

    def test_fingerprint_config_unique_source(self):
        """Test that source must be unique."""
        FingerprintConfig.objects.create(source='prometheus')

        with self.assertRaises(IntegrityError), transaction.atomic():
            FingerprintConfig.objects.create(source='prometheus')

    def test_fingerprint_config_str(self):
        """Test string representation."""
        config = FingerprintConfig.objects.create(source='test-source')
        self.assertEqual(str(config), 'Fingerprint config for test-source')


class ItemRecurrenceTests(TestCase):
    """Test Item recurrence functionality."""

    def test_recurring_task_fields(self):
        """Test recurring task fields."""
        task = Item.objects.create(
            title='Daily Standup',
            is_recurring=True,
            frequency='daily',
            recurrence_pattern='{"time": "09:00", "timezone": "UTC"}'
        )

        self.assertTrue(task.is_recurring)
        self.assertEqual(task.frequency, 'daily')

    def test_get_recurrence_pattern_dict(self):
        """Test getting recurrence pattern as dict."""
        task = Item.objects.create(
            title='Weekly Meeting',
            is_recurring=True,
            frequency='weekly',
            recurrence_pattern='{"day_of_week": "monday", "time": "10:00"}'
        )

        pattern = task.get_recurrence_pattern_dict()
        self.assertIsInstance(pattern, dict)
        self.assertEqual(pattern.get('day_of_week'), 'monday')

    def test_get_recurrence_pattern_dict_invalid_json(self):
        """Test getting recurrence pattern with invalid JSON."""
        task = Item.objects.create(
            title='Bad Pattern',
            recurrence_pattern='not valid json'
        )

        pattern = task.get_recurrence_pattern_dict()
        self.assertEqual(pattern, {})

    def test_get_recurrence_pattern_dict_empty(self):
        """Test getting recurrence pattern when empty."""
        task = Item.objects.create(title='No Pattern')

        pattern = task.get_recurrence_pattern_dict()
        self.assertEqual(pattern, {})


class ItemRelationshipTests(TestCase):
    """Test Item model relationships."""

    def test_parent_child_relationship(self):
        """Test parent-child task relationship."""
        parent = Item.objects.create(
            title='Parent Task',
            is_recurring=True,
            frequency='daily'
        )
        child = Item.objects.create(
            title='Child Task',
            parent_item=parent
        )

        self.assertEqual(child.parent_item, parent)
        self.assertIn(child, parent.child_items.all())

    def test_parent_child_alert_relationship(self):
        """Test parent-child alert relationship."""
        parent_alert = Item.objects.create(
            mode='alert',
            title='Database Down',
            severity='critical'
        )
        child_alert = Item.objects.create(
            mode='alert',
            title='API Timeout',
            parent_alert=parent_alert
        )

        self.assertEqual(child_alert.parent_alert, parent_alert)
        self.assertIn(child_alert, parent_alert.child_alerts.all())

    def test_rule_relationships(self):
        """Test item-rule relationships."""
        approval_rule = ApprovalRule.objects.create(name='Auto Approve')
        silence_rule = SilenceRule.objects.create(name='Maintenance')
        escalation_rule = EscalationRule.objects.create(name='Critical Escalation')

        item = Item.objects.create(
            title='Test Item',
            approval_rule=approval_rule,
            silence_rule=silence_rule,
            escalation_rule=escalation_rule
        )

        self.assertEqual(item.approval_rule, approval_rule)
        self.assertEqual(item.silence_rule, silence_rule)
        self.assertEqual(item.escalation_rule, escalation_rule)


class ItemQuerysetTests(TestCase):
    """Test Item model queryset filtering."""

    def setUp(self):
        """Create test items."""
        # Tasks
        Item.objects.create(mode='task', title='Task 1', status='pending')
        Item.objects.create(mode='task', title='Task 2', status='completed')

        # Actions
        Item.objects.create(mode='action', title='Action 1', status='pending')
        Item.objects.create(mode='action', title='Action 2', status='approved')

        # Alerts
        Item.objects.create(mode='alert', title='Alert 1', status='firing', severity='warning')
        Item.objects.create(mode='alert', title='Alert 2', status='resolved', severity='critical')

    def test_filter_by_mode(self):
        """Test filtering by mode."""
        tasks = Item.objects.filter(mode='task')
        actions = Item.objects.filter(mode='action')
        alerts = Item.objects.filter(mode='alert')

        self.assertEqual(tasks.count(), 2)
        self.assertEqual(actions.count(), 2)
        self.assertEqual(alerts.count(), 2)

    def test_filter_by_status(self):
        """Test filtering by status."""
        # PLS-230: the setUp task created with status='pending' is coerced to
        # 'todo' by the Simple workflow (only task-mode coerces), while the
        # action created with status='pending' is unchanged. So 'pending' now
        # matches only the action, and the task is found under 'todo'.
        todo = Item.objects.filter(status='todo')
        pending = Item.objects.filter(status='pending')
        firing = Item.objects.filter(status='firing')

        self.assertEqual(todo.count(), 1)     # the task (pending -> todo)
        self.assertEqual(pending.count(), 1)  # the action (uncoerced)
        self.assertEqual(firing.count(), 1)

    def test_filter_by_severity(self):
        """Test filtering by severity."""
        warnings = Item.objects.filter(severity='warning')
        criticals = Item.objects.filter(severity='critical')

        self.assertEqual(warnings.count(), 1)
        self.assertEqual(criticals.count(), 1)

    def test_combined_filters(self):
        """Test combined filters."""
        active_alerts = Item.objects.filter(
            mode='alert',
            status__in=['firing', 'acknowledged']
        )

        self.assertEqual(active_alerts.count(), 1)
