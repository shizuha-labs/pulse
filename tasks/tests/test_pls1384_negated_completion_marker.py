"""PLS-1384: negated completion wording must not close a recurring cycle.

Production failure (nova, 2026-09-29, CTX-592 comment 930458): a comment that
BEGINS "Generation 5 / revision 12 — PARTIAL, not cycle complete" and says the
required verification is NOT DONE matched the broad ``cycle … complete``
evidence regex; ``close_recurring_cycle_on_completion_marker`` dispatched
``complete_cycle()`` and Pulse falsely parked the item, advanced the
completion watermark and cycled the revision — hiding genuinely pending work.

Contract under test (Comment -> on_commit -> complete_cycle pipeline, real
TransactionTestCase commits):

1. The verbatim negated production wording does NOT dispatch a completion —
   no status change, no watermark advance, no revision cycle.
2. Affirmative prose still closes (affirmative control).
3. Quoted/example-context markers (fenced, backticked, e.g., blockquote
   history) do NOT close.
4. The structured ``[recurring-cycle complete]`` marker still closes even in
   a comment that also contains negated prose (explicit action, not fuzzy
   prose) — but NOT when the marker itself is inside quoting.
5. Duplicate delivery stays idempotent (one watermark, one generation).
"""

from datetime import timedelta

from django.test import TransactionTestCase
from django.utils import timezone

from tasks.models import Comment, Item, Project, Status
from tasks.services.recurring_cycle_state import cycle_snapshot

# The verbatim production wording (CTX-592 comment 930458, truncated at the
# same header region that matched).
NEGATED_PRODUCTION = (
    "Generation 5 / revision 12 — PARTIAL, not cycle complete. The required "
    "Cortex dashboard comparison is NOT DONE — awaiting authorized read-only "
    "evidence before the cycle can close."
)
AFFIRMATIVE = (
    "All acceptance legs verified against live state — current cycle complete. "
    "Closing revision 12."
)


def _ensure_scheduled_status():
    Status.objects.get_or_create(
        slug='scheduled',
        defaults={
            'name': 'Scheduled', 'category': 'scheduled',
            'color': '#64748b', 'position': 99,
        },
    )


class NegatedCompletionMarkerTests(TransactionTestCase):
    def setUp(self):
        _ensure_scheduled_status()
        self.project = Project.objects.create(
            name='Cortex', key='CTX', org_id=1, access_level='open',
        )

    def _recurring_item(self, **kw):
        kw.setdefault('title', 'weekly cortex dashboard verification')
        kw.setdefault('mode', 'task')
        kw.setdefault('status', 'in_progress')
        kw.setdefault('workflow_name', 'autonomous-bug')
        kw.setdefault('workflow_status', 'In Progress')
        kw.setdefault('is_recurring', True)
        kw.setdefault('frequency', 'weekly')
        kw.setdefault('due_date', timezone.now() - timedelta(hours=1))
        return Item.objects.create(project=self.project, **kw)

    def _assert_untouched(self, item, before):
        """No state write happened: still active, same generation/watermark."""
        item.refresh_from_db()
        self.assertNotEqual(item.status, 'scheduled')
        snap = cycle_snapshot(item)
        self.assertEqual(snap['generation'], before['generation'])
        self.assertEqual(snap['completion_watermark'], before['completion_watermark'])

    def test_negated_production_wording_does_not_close(self):
        """The exact CTX-592 failure shape: 'PARTIAL, not cycle complete'."""
        item = self._recurring_item()
        before = cycle_snapshot(item)
        Comment.objects.create(
            item=item, author_id=0, author_email='nova@agents.shizuha.io',
            content=NEGATED_PRODUCTION,
        )
        self._assert_untouched(item, before)

    def test_negation_inside_span_does_not_close(self):
        item = self._recurring_item()
        before = cycle_snapshot(item)
        Comment.objects.create(
            item=item, author_id=0, author_email='nova@agents.shizuha.io',
            content="The cycle is not complete — dashboard evidence still pending.",
        )
        self._assert_untouched(item, before)

    def test_affirmative_prose_still_closes(self):
        item = self._recurring_item()
        Comment.objects.create(
            item=item, author_id=0, author_email='nova@agents.shizuha.io',
            content=AFFIRMATIVE,
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')
        cycle = item.metadata['recurring_cycle']
        # cycle_snapshot exposes generation, not completed_generation.
        self.assertEqual(cycle['completed_generation'], cycle['active_generation'])
        self.assertEqual(cycle['consecutive_skips'], 0)

    def test_quoted_and_example_markers_do_not_close(self):
        item = self._recurring_item()
        before = cycle_snapshot(item)
        Comment.objects.create(
            item=item, author_id=0, author_email='nova@agents.shizuha.io',
            content=(
                "Reminder for closers — never write bare prose, use the "
                "transition. Example forms that DO NOT close:\n"
                "```\ncycle complete\n```\n"
                "`cycle complete`\n"
                "> cycle 4 complete (historical)\n"
                "e.g. cycle complete would match the old parser."
            ),
        )
        self._assert_untouched(item, before)

    def test_structured_marker_closes_despite_negated_prose(self):
        """Explicit action beats prose ambiguity: a comment that reports the
        cycle PARTIAL *and* posts the structured marker closes via the marker."""
        item = self._recurring_item()
        Comment.objects.create(
            item=item, author_id=0, author_email='nova@agents.shizuha.io',
            content=(
                "Generation 5 / revision 12 — PARTIAL, not cycle complete in the "
                "earlier attempt; [recurring-cycle complete] re-affirmed after "
                "the dashboard evidence landed."
            ),
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')

    def test_structured_marker_inside_backticks_is_fail_safe(self):
        """Quoting the marker is intent, not the act — no close."""
        item = self._recurring_item()
        before = cycle_snapshot(item)
        Comment.objects.create(
            item=item, author_id=0, author_email='nova@agents.shizuha.io',
            content="Post `[recurring-cycle complete]` once evidence lands.",
        )
        self._assert_untouched(item, before)

    def test_duplicate_delivery_converges(self):
        item = self._recurring_item()
        Comment.objects.create(
            item=item, author_id=0, author_email='nova@agents.shizuha.io',
            content=AFFIRMATIVE,
        )
        item.refresh_from_db()
        first = cycle_snapshot(item)
        # Duplicate delivery (webhook replay) — no second watermark/generation.
        Comment.objects.create(
            item=item, author_id=0, author_email='nova@agents.shizuha.io',
            content=AFFIRMATIVE,
        )
        item.refresh_from_db()
        second = cycle_snapshot(item)
        self.assertEqual(item.status, 'scheduled')
        self.assertEqual(second['generation'], first['generation'])
        self.assertEqual(
            second['completion_watermark'], first['completion_watermark'],
        )

    def test_new_generation_ignores_quoted_history_and_rearms_on_affirmative(self):
        """A re-armed generation must not close on quoted prior-cycle text.
        A fresh affirmative comment resets skips and completes that generation."""
        item = self._recurring_item()
        Comment.objects.create(
            item=item, author_id=0, author_email='nova@agents.shizuha.io',
            content=AFFIRMATIVE,
        )
        item.refresh_from_db()
        cycle = dict(item.metadata['recurring_cycle'])
        cycle['active_generation'] = cycle['active_generation'] + 1
        cycle['consecutive_skips'] = 3
        item.metadata['recurring_cycle'] = cycle
        item.status = 'in_progress'
        item.workflow_status = 'In Progress'
        item.save(update_fields=['metadata', 'status', 'workflow_status', 'updated_at'])
        before = cycle_snapshot(item)
        Comment.objects.create(
            item=item, author_id=0, author_email='nova@agents.shizuha.io',
            content=(
                "Prior cycle text, not this generation:\n"
                "> cycle 4 complete (historical)\n"
                "`cycle complete`\n"
            ),
        )
        self._assert_untouched(item, before)
        item.refresh_from_db()
        self.assertEqual(item.metadata['recurring_cycle']['consecutive_skips'], 3)
        Comment.objects.create(
            item=item, author_id=0, author_email='nova@agents.shizuha.io',
            content=AFFIRMATIVE,
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')
        cycle = item.metadata['recurring_cycle']
        self.assertEqual(cycle['completed_generation'], cycle['active_generation'])
        self.assertEqual(cycle['consecutive_skips'], 0)
