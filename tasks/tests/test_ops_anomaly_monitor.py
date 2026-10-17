from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from tasks.models import Comment, Item, ItemActivity, Notification, Project


class PulseOpsAnomalyMonitorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.project = Project.objects.create(
            name='Platform',
            key='PLAT',
            org_id=1,
            lead_id=11,
            default_assignee_id=22,
        )
        self.item = Item.objects.create(
            mode='task',
            item_type='task.bug',
            title='Noisy automation target',
            status='in_progress',
            priority='high',
            project=self.project,
            item_key='PLAT-207',
            assignment_group='devops',
            workflow_name='autonomous-bug',
        )

    @override_settings(
        PULSE_OPS_COMMENT_ITEM_HOURLY_THRESHOLD=3,
        PULSE_OPS_ACTIVITY_ITEM_HOURLY_THRESHOLD=999,
        PULSE_OPS_ACTOR_FANOUT_DISTINCT_ITEMS_THRESHOLD=999,
        PULSE_OPS_IDENTICAL_NOTIFICATION_THRESHOLD=999,
    )
    def test_comment_storm_replay_creates_owner_group_alert(self):
        """Synthetic PLS-207-style repeated comments fire from the write path."""
        Comment.objects.create(
            item=self.item,
            author_id=7,
            author_email='system@pulse',
            content='auto-failover note 1',
        )
        Comment.objects.create(
            item=self.item,
            author_id=7,
            author_email='system@pulse',
            content='auto-failover note 2',
        )

        with mock.patch('tasks.tasks.send_alert_notification.delay') as notify_delay:
            with self.captureOnCommitCallbacks(execute=True):
                Comment.objects.create(
                    item=self.item,
                    author_id=7,
                    author_email='system@pulse',
                    content='auto-failover note 3',
                )

        alert = Item.objects.get(mode='alert', source='pulse-ops-anomaly')
        self.assertEqual(alert.assignment_group, 'devops')
        self.assertEqual(alert.project, self.project)
        self.assertEqual(alert.labels['rule'], 'per_item_comment_rate')
        self.assertEqual(alert.labels['item_key'], 'PLAT-207')
        self.assertEqual(alert.payload['count'], 3)
        notify_delay.assert_called_once_with(alert.id)

    @override_settings(
        PULSE_OPS_COMMENT_ITEM_HOURLY_THRESHOLD=999,
        PULSE_OPS_ACTIVITY_ITEM_HOURLY_THRESHOLD=3,
        PULSE_OPS_ACTOR_FANOUT_DISTINCT_ITEMS_THRESHOLD=999,
        PULSE_OPS_IDENTICAL_NOTIFICATION_THRESHOLD=999,
    )
    def test_activity_db_confirmation_is_gated_by_cache_counter(self):
        """Below the cheap cache threshold, the hot write path avoids DB counts."""
        from tasks.services.ops_anomaly_monitor import observe_item_activity_created

        activity = ItemActivity(
            item=self.item,
            action='updated',
            user_id=101,
            user_email='bot@agents.shizuha.io',
        )
        with mock.patch('tasks.models.ItemActivity.objects.filter') as filter_mock:
            observe_item_activity_created(activity)
        filter_mock.assert_not_called()

    def test_detector_exception_emits_fail_loud_owner_alert(self):
        """Detector failures are non-fatal to writes but still notify DevOps."""
        with mock.patch(
            'tasks.services.ops_anomaly_monitor.observe_comment_created',
            side_effect=RuntimeError('detector broke'),
        ), mock.patch('tasks.tasks.send_alert_notification.delay') as notify_delay:
            with self.captureOnCommitCallbacks(execute=True):
                Comment.objects.create(
                    item=self.item,
                    author_id=7,
                    author_email='system@pulse',
                    content='trigger detector exception',
                )

        alert = Item.objects.get(mode='alert', source='pulse-ops-anomaly')
        self.assertEqual(alert.assignment_group, 'devops')
        self.assertEqual(alert.labels['rule'], 'detector_exception')
        self.assertEqual(alert.labels['detector'], 'comment')
        self.assertIn('fail-loud', alert.description)
        notify_delay.assert_called_once_with(alert.id)

    @override_settings(
        PULSE_OPS_COMMENT_ITEM_HOURLY_THRESHOLD=999,
        PULSE_OPS_ACTIVITY_ITEM_HOURLY_THRESHOLD=999,
        PULSE_OPS_ACTOR_FANOUT_DISTINCT_ITEMS_THRESHOLD=3,
        PULSE_OPS_ACTOR_FANOUT_WINDOW_SECONDS=60,
        PULSE_OPS_IDENTICAL_NOTIFICATION_THRESHOLD=999,
    )
    def test_actor_fanout_across_distinct_items_creates_alert(self):
        items = [self.item]
        for idx in range(2):
            items.append(Item.objects.create(
                mode='task',
                item_type='task.bug',
                title=f'fanout target {idx}',
                status='in_progress',
                priority='normal',
                project=self.project,
                item_key=f'PLAT-F{idx}',
                assignment_group='devops',
                workflow_name='autonomous-bug',
            ))

        ItemActivity.objects.create(item=items[0], action='updated', user_id=101, user_email='bot@agents.shizuha.io')
        ItemActivity.objects.create(item=items[1], action='updated', user_id=101, user_email='bot@agents.shizuha.io')

        with mock.patch('tasks.tasks.send_alert_notification.delay') as notify_delay:
            with self.captureOnCommitCallbacks(execute=True):
                ItemActivity.objects.create(item=items[2], action='updated', user_id=101, user_email='bot@agents.shizuha.io')

        alert = Item.objects.get(mode='alert', source='pulse-ops-anomaly')
        self.assertEqual(alert.labels['rule'], 'per_actor_activity_fanout')
        self.assertEqual(alert.labels['actor_id'], '101')
        self.assertEqual(alert.payload['distinct_items'], 3)
        notify_delay.assert_called_once_with(alert.id)

    @override_settings(
        PULSE_OPS_COMMENT_ITEM_HOURLY_THRESHOLD=999,
        PULSE_OPS_ACTIVITY_ITEM_HOURLY_THRESHOLD=999,
        PULSE_OPS_ACTOR_FANOUT_DISTINCT_ITEMS_THRESHOLD=999,
        PULSE_OPS_IDENTICAL_NOTIFICATION_THRESHOLD=3,
        PULSE_OPS_IDENTICAL_NOTIFICATION_WINDOW_SECONDS=300,
    )
    def test_identical_notification_storm_creates_alert(self):
        for _ in range(2):
            Notification.objects.create(
                user_id=55,
                item=self.item,
                notification_type='comment',
                title='Task Update',
                message='Same message body',
            )

        with mock.patch('tasks.tasks.send_alert_notification.delay') as notify_delay:
            with self.captureOnCommitCallbacks(execute=True):
                Notification.objects.create(
                    user_id=55,
                    item=self.item,
                    notification_type='comment',
                    title='Task Update',
                    message='Same message body',
                )

        alert = Item.objects.get(mode='alert', source='pulse-ops-anomaly')
        self.assertEqual(alert.labels['rule'], 'identical_notification_storm')
        self.assertEqual(alert.labels['recipient_user_id'], '55')
        self.assertEqual(alert.payload['count'], 3)
        notify_delay.assert_called_once_with(alert.id)
