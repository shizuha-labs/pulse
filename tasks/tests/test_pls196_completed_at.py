"""PLS-196: completed_at follows workflow done-category completion."""
import importlib

from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Status, ItemActivity


class CompletedAtStatusInvariantTests(TestCase):
    def setUp(self):
        Status.objects.get_or_create(slug='open', defaults={'name': 'Open', 'category': 'todo'})
        Status.objects.get_or_create(slug='done', defaults={'name': 'Done', 'category': 'done'})
        Status.objects.get_or_create(slug='cancelled', defaults={'name': 'Cancelled', 'category': 'done'})

    def test_save_stamps_completed_at_when_entering_done_category(self):
        item = Item.objects.create(mode='task', title='Complete me', status='open', workflow_name='simple')
        self.assertIsNone(item.completed_at)

        item.status = 'done'
        item.save(update_fields=['status'])

        item.refresh_from_db()
        self.assertEqual(item.status, 'done')
        self.assertIsNotNone(item.completed_at)

    def test_save_preserves_existing_done_timestamp_and_clears_when_reopened(self):
        completed_at = timezone.now() - timezone.timedelta(days=2)
        item = Item.objects.create(
            mode='task',
            title='Already done',
            status='done',
            workflow_name='simple',
            completed_at=completed_at,
        )
        item.refresh_from_db()
        self.assertEqual(item.completed_at, completed_at)

        item.title = 'Still done'
        item.save(update_fields=['title'])
        item.refresh_from_db()
        self.assertEqual(item.completed_at, completed_at)

        item.status = 'open'
        item.save(update_fields=['status'])
        item.refresh_from_db()
        self.assertIsNone(item.completed_at)

    def test_all_done_category_statuses_stamp_completed_at(self):
        # 'simple' has no 'cancelled' status, so the assignment would coerce away;
        # use an unseeded workflow (no coercion) — completed_at stamping keys off
        # the global Status category, not the workflow.
        item = Item.objects.create(mode='task', title='Cancelled terminal', status='open', workflow_name='autonomous-dev')

        item.status = 'cancelled'
        item.save(update_fields=['status'])

        item.refresh_from_db()
        self.assertEqual(item.status, 'cancelled')
        self.assertIsNotNone(item.completed_at)

    def test_backfill_uses_item_activity_transition_timestamp(self):
        item = Item.objects.create(mode='task', title='Historical done', status='open', workflow_name='simple')
        transition_at = timezone.now() - timezone.timedelta(hours=5)
        Item.objects.filter(pk=item.pk).update(status='done', workflow_status='Done', completed_at=None)
        activity = ItemActivity.objects.create(
            item=item,
            action='status_changed',
            field_name='Status',
            old_value='Open',
            new_value='Done',
            user_id=0,
            user_email='system@pulse',
        )
        ItemActivity.objects.filter(pk=activity.pk).update(created_at=transition_at)

        migration = importlib.import_module('tasks.migrations.0074_pls196_backfill_completed_at')

        migration.backfill_completed_at(type('Apps', (), {'get_model': staticmethod(lambda app, model: {
            'Item': Item,
            'ItemActivity': ItemActivity,
            'Status': Status,
        }[model])})(), None)

        item.refresh_from_db()
        self.assertEqual(item.completed_at, transition_at)
