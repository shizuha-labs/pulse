"""PLAT-11947: sweep verdict posts are fenced by a generation lease."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project, StaleSweepWriter, SweepLeaseExpired
from tasks.services.sweep_lease import (
    acquire_sweep_lease,
    post_sweep_verdict,
)
from tasks.tasks import _post_fenced_sweep_verdict


class SweepLeaseTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Lease', key='LEASE', org_id=1)
        self.item = Item.objects.create(
            mode='task', title='lease target', project=self.project,
            item_key='LEASE-11947', status='pending',
        )

    def test_newer_generation_wins_and_stale_writer_is_typed(self):
        acquire_sweep_lease(self.item, 'worker-a', 2)
        with self.assertRaises(StaleSweepWriter) as caught:
            acquire_sweep_lease(self.item, 'worker-b', 1)
        self.assertEqual(caught.exception.code, 'stale_sweep_writer')
        self.assertEqual(caught.exception.held_generation, 2)
        self.assertEqual(caught.exception.attempted_generation, 1)
        event = caught.exception.as_event()
        self.assertEqual(event['event'], 'stale_sweep_writer')
        self.assertEqual(event['item_key'], 'LEASE-11947')

    def test_same_generation_other_holder_is_rejected(self):
        acquire_sweep_lease(self.item, 'worker-a', 3)
        with self.assertRaises(StaleSweepWriter):
            acquire_sweep_lease(self.item, 'worker-b', 3)

    def test_lapsed_lease_expires_loudly_once(self):
        lease = acquire_sweep_lease(self.item, 'worker-a', 1)
        lease.expires_at = timezone.now() - timedelta(seconds=1)
        lease.save(update_fields=['expires_at'])
        with self.assertRaises(SweepLeaseExpired) as caught:
            acquire_sweep_lease(self.item, 'worker-b', 2)
        self.assertEqual(caught.exception.code, 'sweep_lease_expired')
        lease.refresh_from_db()
        self.assertTrue(lease.expired_loudly)
        # The loud expiry is once. The next acquire reopens the slot.
        reopened = acquire_sweep_lease(self.item, 'worker-b', 2)
        self.assertEqual(reopened.holder, 'worker-b')
        self.assertEqual(reopened.generation, 2)
        self.assertFalse(reopened.expired_loudly)
        self.assertGreater(reopened.expires_at, timezone.now())

    def test_verdict_post_requires_current_holder(self):
        acquire_sweep_lease(self.item, 'worker-a', 4)
        calls = []
        post_sweep_verdict(self.item, 'worker-a', 4, lambda locked: calls.append(locked.pk))
        self.assertEqual(calls, [self.item.pk])
        with self.assertRaises(StaleSweepWriter):
            post_sweep_verdict(
                self.item, 'worker-b', 4, lambda locked: calls.append('lost'),
            )
        self.assertEqual(calls, [self.item.pk])

    def test_verdict_post_on_lapse_does_not_call_writer(self):
        lease = acquire_sweep_lease(self.item, 'worker-a', 5)
        lease.expires_at = timezone.now() - timedelta(seconds=1)
        lease.save(update_fields=['expires_at'])
        with self.assertRaises(SweepLeaseExpired):
            post_sweep_verdict(self.item, 'worker-a', 5, lambda locked: (_ for _ in ()).throw(AssertionError('posted')))
        lease.refresh_from_db()
        self.assertTrue(lease.expired_loudly)

    def test_fenced_helper_logs_loss_and_continues(self):
        acquire_sweep_lease(self.item, 'worker-a', 7)
        with mock.patch('tasks.tasks.logger') as logger:
            result = _post_fenced_sweep_verdict(
                self.item, 'worker-b', 1, lambda locked: 'posted',
            )
        self.assertIsNone(result)
        self.assertTrue(logger.warning.called)
        with self.assertRaises(StaleSweepWriter):
            post_sweep_verdict(self.item, 'worker-b', 7, lambda locked: None)
