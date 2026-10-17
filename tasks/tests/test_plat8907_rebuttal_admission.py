"""PLAT-8907: well-formed PLAT-202 rebuttals must be admitted — and rejection
messages must diagnose why one wasn't.

Live defect (PLAT-8894, 09-16): three well-formed scoped rebuttals naming an
unchanged duplicate PR were rejected identically before the submission was
admitted on a later attempt, with no diagnostic. Root cause pinned to the
rebuttal scan's AUTHOR filter: fleet agents authenticate under two
identity-equivalent spellings (`@shizuha.com` primary / `@agents.shizuha.io`
internal alias), and failover churn rewrote the item's assignee between the
bounce and the rebuttal — so rebuttals authored under the other spelling were
silently dropped by `author_email__in`. A secondary latent defect: a bounce
key stored in the legacy singular `/pull/N` form parsed as untrusted, making
the PR permanently unnameable.

These tests pin:
  - a well-formed scoped rebuttal is admitted when its author spelling differs
    from the context's spelling but is the SAME principal (alias fix);
  - a legacy singular `/pull/N` bounce key is nameable (coords fix);
  - the rejection message carries the PLAT-8907 diagnostic when a
    marker-carrying post-bounce comment exists but was not admitted.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from tasks.models import Comment, Item, Workflow
from tasks.services.workflow_service import WorkflowService

_has_rebuttal = WorkflowService._has_review_rebuttal_since_bounce
_reentry = WorkflowService._review_reentry_has_progress

_PULSE_URL = 'https://origin.shizuha.com/shizuha-labs/pulse/pulls/519'
_LEGACY_SINGULAR_URL = 'https://origin.shizuha.com/shizuha-labs/pulse/pull/519'


def _stamp_bounce(item, heads):
    meta = dict(item.metadata or {})
    meta['review_bounce_at'] = timezone.now().isoformat()
    meta['review_bounce_pr_heads'] = heads
    item.metadata = meta
    item.save(update_fields=['metadata'])


class AliasAuthorRebuttalAdmissionTests(TestCase):
    """The alias fix: rebuttal authority is principal-based, not spelling-based."""

    def _item(self, assignee_email):
        return Item.objects.create(
            title='gate-layer fix', mode='task', workflow_name='simple',
            status='in_progress', metadata={},
            assignee_id=8907, assignee_email=assignee_email,
            created_by_id=1, created_by_email='goro@agents.shizuha.io',
        )

    def test_rebuttal_admitted_across_alias_spelling(self):
        """Bounce stamps the context under `@shizuha.com` (failover-rewritten);
        the durable author then rebutts under `@agents.shizuha.io` — the SAME
        principal. The scan must admit it (PLAT-8894's exact failure shape)."""
        item = self._item(assignee_email='jun@shizuha.com')
        _stamp_bounce(item, {_PULSE_URL: 'ad01cabfad73'})

        Comment.objects.create(
            item=item, author_id=8907, author_email='jun@agents.shizuha.io',
            content=(
                'PLAT-202 rebuttal: shizuha-labs/pulse#519 — unchanged because '
                'it is the duplicate parallel implementation, not pending work.'
            ),
        )

        self.assertTrue(
            _has_rebuttal(item, scoped_to={_PULSE_URL}),
            'a same-principal rebuttal under the alias spelling must be admitted',
        )

    def test_rebuttal_admitted_primary_to_alias_direction(self):
        """The inverse direction: context holds the `@agents.shizuha.io`
        spelling, rebuttal authored under `@shizuha.com`."""
        item = self._item(assignee_email='jun@agents.shizuha.io')
        _stamp_bounce(item, {_PULSE_URL: 'ad01cabfad73'})

        Comment.objects.create(
            item=item, author_id=8907, author_email='jun@shizuha.com',
            content='PLAT-202 rebuttal: shizuha-labs/pulse#519 — duplicate PR, canonical fix is #520.',
        )

        self.assertTrue(_has_rebuttal(item, scoped_to={_PULSE_URL}))

    def test_unrelated_author_still_rejected(self):
        """The alias widening must not fail open: a genuinely different
        principal's rebuttal is still not authority."""
        item = self._item(assignee_email='jun@shizuha.com')
        _stamp_bounce(item, {_PULSE_URL: 'ad01cabfad73'})

        Comment.objects.create(
            item=item, author_id=42, author_email='someone-else@agents.shizuha.io',
            content='PLAT-202 rebuttal: shizuha-labs/pulse#519 — looks fine to me.',
        )

        self.assertFalse(_has_rebuttal(item, scoped_to={_PULSE_URL}))


class LegacySingularBounceKeyTests(TestCase):
    """The coords fix: a legacy singular `/pull/N` bounce key is nameable."""

    def test_singular_url_key_is_trusted(self):
        """`_key_coords` accepts the legacy singular Origin URL — previously it
        parsed to None, which appended NO patterns and made the PR permanently
        unnameable regardless of rebuttal text. Exercise through the real scan
        with a real item + comment (no provider calls — `_has_rebuttal` is the
        scan only)."""
        item = Item.objects.create(
            title='legacy singular bounce key', mode='task',
            workflow_name='simple', status='in_progress', metadata={},
            assignee_id=8907, assignee_email='jun@agents.shizuha.io',
            created_by_id=1, created_by_email='goro@agents.shizuha.io',
        )
        item.metadata = {
            'review_bounce_at': timezone.now().isoformat(),
            'review_bounce_pr_heads': {_LEGACY_SINGULAR_URL: 'ad01cabfad73'},
        }
        item.save(update_fields=['metadata'])
        Comment.objects.create(
            item=item, author_id=8907, author_email='jun@agents.shizuha.io',
            content='PLAT-202 rebuttal: shizuha-labs/pulse#519 — duplicate, canonical is #520.',
        )

        self.assertTrue(
            _has_rebuttal(item, scoped_to={_LEGACY_SINGULAR_URL}),
            'a legacy singular /pull/N bounce key must be nameable by owner/repo#N',
        )


class RejectionDiagnosticTests(TestCase):
    """Ask 3: the rejection message distinguishes 'no rebuttal posted' from
    'rebuttal posted but not admitted'."""

    def _bounced_item(self):
        # TWO unchanged PRs: on single-PR tasks the durable-author generic
        # marker shortcut admits any marker comment without naming the PR, so
        # explicit-scope enforcement (and thus the scope-mismatch diagnostic)
        # is only observable on a multi-PR obligation set.
        second_url = 'https://origin.shizuha.com/shizuha-labs/pulse/pulls/518'
        item = Item.objects.create(
            item_key='PLAT-8907-T1',
            title='gate fix with unchanged duplicates', mode='task',
            workflow_name='autonomous-dev', status='in_progress',
            workflow_status='in_progress',
            assignee_id=8907, assignee_email='jun@shizuha.com',
            created_by_id=1, created_by_email='goro@agents.shizuha.io',
            metadata={
                'pull_requests': [
                    {'url': _PULSE_URL, 'repo': 'shizuha-labs/pulse',
                     'number': 519, 'status': 'open',
                     'head_sha': 'ad01cabfad73'},
                    {'url': second_url, 'repo': 'shizuha-labs/pulse',
                     'number': 518, 'status': 'open',
                     'head_sha': 'ad01cabfad72'},
                ],
                'review_bounce_at': timezone.now().isoformat(),
                'review_bounce_pr_heads': {
                    _PULSE_URL: 'ad01cabfad73',
                    second_url: 'ad01cabfad72',
                },
            },
        )
        return item

    def test_diagnostic_names_existing_marker_comment(self):
        """A marker-carrying post-bounce comment that was NOT admitted (here:
        scope mismatch — it names a different PR) surfaces the DIAGNOSTIC with
        the comment's author, instead of bouncing identically to no-progress."""
        from tasks.tests.test_plat3159_review_reentry import _live_confirm

        item = self._bounced_item()
        Comment.objects.create(
            item=item, author_id=8907, author_email='jun@agents.shizuha.io',
            content='PLAT-202 rebuttal: shizuha-labs/pulse#999 — wrong number.',
        )

        with _live_confirm(item):
            ok, detail = _reentry(item)
        self.assertFalse(ok)
        self.assertIn('DIAGNOSTIC', detail)
        self.assertIn('jun@agents.shizuha.io', detail)
        self.assertIn('not admitted', detail)

    def test_no_diagnostic_when_no_post_bounce_comments(self):
        """Genuine no-progress retries stay quiet: no post-bounce comment at
        all, no diagnostic noise."""
        from tasks.tests.test_plat3159_review_reentry import _live_confirm

        item = self._bounced_item()

        with _live_confirm(item):
            ok, detail = _reentry(item)
        self.assertFalse(ok)
        self.assertNotIn('DIAGNOSTIC', detail)
