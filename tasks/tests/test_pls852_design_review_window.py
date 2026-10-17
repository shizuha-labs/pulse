"""PLS-852 — the design-review broadcast window must be computed once per review
version, not per emit.

The `connect_group_post` post-function fires on every transition into
Architecture Review, and a transition can be re-fired while a review cannot be
assigned. Each re-fire used to recompute ``now + 48h``, so the advertised
deadline drifted forward on every notice and no notice was authoritative.

The fix stores the close time on the item's metadata at first emit, reuses it on
re-emits (marked as reminders), and clears it when the item leaves in_review so
a genuinely new review entry gets a fresh window.
"""
from unittest import mock

from django.test import TestCase, override_settings

from tasks.models import Item


def _make_item(**kw):
    defaults = dict(
        title='ORIG-240 design review',
        description='A design review task.\nSecond line.',
        status='in_review',
        workflow_status='in_review',
        item_key='ORIG-240',
        mode='task',
    )
    defaults.update(kw)
    return Item.objects.create(**defaults)


class DesignReviewWindowTests(TestCase):
    def setUp(self):
        self.item = _make_item()

    def _post(self, item):
        from tasks.tasks import post_design_review_thread

        with mock.patch('tasks.tasks._post_to_design_review_group') as post:
            result = post_design_review_thread.run(item.id, 'conv-1')
        return result, post

    def test_window_is_stored_on_first_emit_and_reused_on_reemit(self):
        first, post1 = self._post(self.item)
        self.item.refresh_from_db()
        self.assertFalse(first['reemit'])
        self.assertEqual(post1.call_count, 1)
        stored_close = self.item.metadata['design_review_window_close']
        self.assertIn(stored_close, post1.call_args.args[1])

        # A re-emit (same review, transition re-fired) must reuse the stored
        # window — not recompute it — and be explicitly marked as a reminder.
        second, post2 = self._post(self.item)
        self.item.refresh_from_db()
        self.assertTrue(second['reemit'])
        self.assertEqual(post2.call_count, 1)
        self.assertIn(stored_close, post2.call_args.args[1])
        self.assertIn('Reminder', post2.call_args.args[1])
        self.assertEqual(self.item.metadata['design_review_window_close'], stored_close)

    def test_workflow_service_clears_window_when_leaving_in_review(self):
        """The transition executor must clear the stored window on exit from
        in_review, or a genuinely new review entry would inherit a stale
        deadline from the prior review."""
        from tasks.services.workflow_service import WorkflowService

        # Seed a stored window (as post_design_review_thread would).
        meta = dict(self.item.metadata or {})
        meta['design_review_window_close'] = '5 Aug 15:36 UTC'
        self.item.metadata = meta
        self.item.save(update_fields=['metadata', 'updated_at'])

        # Leaving in_review (status change) clears the key. Simulate the
        # transition context: _original_status is the pre-transition status.
        self.item._original_status = 'in_review'
        WorkflowService._clear_design_review_window_on_exit(self.item, 'in_progress')
        self.assertNotIn('design_review_window_close', self.item.metadata)

        # Staying in_review (re-emit) preserves it.
        meta = dict(self.item.metadata or {})
        meta['design_review_window_close'] = '5 Aug 15:36 UTC'
        self.item.metadata = meta
        self.item.save(update_fields=['metadata', 'updated_at'])
        self.item._original_status = 'in_review'
        WorkflowService._clear_design_review_window_on_exit(self.item, 'in_review')
        self.assertEqual(self.item.metadata['design_review_window_close'], '5 Aug 15:36 UTC')

    @override_settings(DESIGN_REVIEW_WINDOW_HOURS=24)
    def test_window_hours_setting_is_respected(self):
        from datetime import timedelta

        from django.utils import timezone

        from tasks.tasks import post_design_review_thread

        with mock.patch('tasks.tasks._post_to_design_review_group') as post:
            post_design_review_thread.run(self.item.id, 'conv-1')
        content = post.call_args.args[1]
        expected = (timezone.now() + timedelta(hours=24)).strftime('%-d %b %H:%M UTC')
        self.assertIn(f'closes {expected}', content)
