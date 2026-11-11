"""PLAT-10536 regressions: the weekly design-review digest's 'in review' age
must read the transition INTO the review status (latest status_changed
ItemActivity into the current status), NOT updated_at — any comment/field
touch resets updated_at, so actively-discussed reviews showed 0d forever.
Rows must render longest-waiting first.

CI lessons baked in (runs 7916/7918/7920): the task reads its conversation
id from settings (no kwargs); fixtures must carry the design-review
classifier signal; ItemActivity.created_at is auto_now_add so tests drive
age via a mocked Django clock (backdating kwargs are silently ignored)."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from tasks.models import Item, ItemActivity


def _make_item(item_key, **kw):
    defaults = dict(
        title=f'{item_key} design review',
        description='A design review task.\nSecond line.',
        status='in_review',
        workflow_status='in_review',
        item_key=item_key,
        mode='task',
        # workflow_name MUST be one whose seed contains 'in_review' —
        # Item.save()'s _coerce_invalid_status heals any slug absent from the
        # workflow (simple = todo/in_progress/done → 'in_review' coerced to
        # 'todo' under real migrations, CI run 7922: digest empty). The
        # 'software-development' system workflow seeds in_review (0023).
        # sqlite --no-migrations has no workflow rows, so the coercion no-ops
        # there — the local/CI divergence this fixes.
        workflow_name='software-development',
        # The digest's _is_design_review_item classifier keys on labels/tags
        # (hld/adr/design family) or an HLD:/ADR: title prefix — without the
        # signal the item is correctly excluded and the digest renders its
        # empty state (CI run 7918).
        labels=['design'],
    )
    defaults.update(kw)
    return Item.objects.create(**defaults)


def _status_change(item, new_value, field_name='Status'):
    """Record a transition activity. created_at is auto_now_add — the clock
    fixture, not kwargs, positions it in time."""
    ItemActivity.objects.create(
        item=item, action='status_changed', field_name=field_name,
        old_value='in_progress', new_value=new_value,
        user_id=1, user_email='t@x.io',
    )


class _Clock:
    """Mock Django clock: creation/activities stamp at clock time; advance()
    moves the digest's now forward. Replaces ignored created_at kwargs (the
    auto_now_add trap CI run 7920 exposed)."""

    def __init__(self):
        self.now = timezone.now()

    def advance(self, **kw):
        self.now = self.now + timedelta(**kw)

    def __call__(self):
        return self.now


class DigestReviewAgeTests(TestCase):
    """The clock patch spans the WHOLE test (setUp): auto_now_add stamps on
    Item/ItemActivity and the digest's age math must all consult the same
    fake now — a _run_digest-scoped patch leaves creation stamps at real
    time and ages come out inconsistent."""

    def setUp(self):
        self.clock = _Clock()
        self._settings = override_settings(DESIGN_REVIEW_CONVERSATION_ID='conv-test')
        self._settings.enable()
        self._clock_patch = mock.patch('django.utils.timezone.now', self.clock)
        self._clock_patch.start()
        self.addCleanup(self._clock_patch.stop)
        self.addCleanup(self._settings.disable)
        self._poster = mock.patch('tasks.tasks._post_to_design_review_group')
        self.post = self._poster.start()
        self.post.return_value.status_code = 200
        self.addCleanup(self._poster.stop)

    def _run_digest(self):
        from tasks.tasks import post_weekly_design_review_digest
        result = post_weekly_design_review_digest.apply().get()
        return result, self.post.call_args[0][1]

    def test_age_uses_transition_not_updated_at(self):
        """A touch at digest time resets updated_at; the rendered age still
        reads the review-entry transition (9 days earlier)."""
        it = _make_item('ORIG-240')
        _status_change(it, 'in_review')
        self.clock.advance(days=9)
        it.updated_at = self.clock()  # touched at digest time — 0d ago
        it.save(update_fields=['updated_at'])

        result, body = self._run_digest()
        self.assertTrue(result['posted'])
        self.assertIn('in review 9d', body)
        self.assertNotIn('in review 0d', body)

    def test_fallback_created_at_when_no_transition_recorded(self):
        """Items minted directly into the review lane (orphan class) with no
        status_changed activity age from created_at."""
        _make_item('ORIG-241')
        self.clock.advance(days=5)
        it = Item.objects.get(item_key='ORIG-241')
        it.updated_at = self.clock()
        it.save(update_fields=['updated_at'])

        result, body = self._run_digest()
        self.assertIn('in review 5d', body)

    def test_rows_sort_longest_waiting_first(self):
        """Ordering follows review-entry age (longest first)."""
        older = _make_item('ORIG-242')
        _status_change(older, 'in_review')
        self.clock.advance(days=10)  # 243 enters review 10 days later
        newer = _make_item('ORIG-243')
        _status_change(newer, 'in_review')
        self.clock.advance(days=2)   # digest: 242 has waited 12d, 243 2d

        result, body = self._run_digest()
        self.assertLess(body.index('ORIG-242'), body.index('ORIG-243'))

    def test_lowercase_field_name_writer_is_seen(self):
        """Some writers record field_name='status' (lowercase); the iexact
        filter must see both casings."""
        it = _make_item('ORIG-244')
        _status_change(it, 'in_review', field_name='status')
        self.clock.advance(days=6)

        result, body = self._run_digest()
        self.assertIn('in review 6d', body)
