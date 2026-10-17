"""PLS-510: review churn circuit breaker.

Production-order fixture (sora 2026-08-01 on PLS-675 escape):
  1. ≥2 Request Changes at exact head H
  2. Unchanged rebuttal at H with unresolved findings / red premerge
  3. Submit-for-Review must fail closed
  4. New head H2 + complete structured finding responses must remain admissible
"""
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService

ORIGIN_PR = {
    'url': 'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/166',
    'number': 166,
    'repo': 'shizuha-labs/shizuha-tasks',
    'head_sha': 'a' * 40,
    'status': 'open',
}
HEAD_H = 'a' * 40
HEAD_H2 = 'b' * 40


class ReviewChurnBreakerTests(SimpleTestCase):
    def test_stamp_increments_same_head_count(self):
        item = SimpleNamespace(metadata={})
        with patch.object(
            WorkflowService,
            '_linked_pr_entries',
            return_value=[dict(ORIGIN_PR)],
        ):
            WorkflowService._stamp_review_bounce(item)
            self.assertEqual(item.metadata.get('review_bounce_same_head_count'), 1)
            WorkflowService._stamp_review_bounce(item)
            self.assertEqual(item.metadata.get('review_bounce_same_head_count'), 2)

    def test_stamp_resets_count_and_clears_responses_on_new_head(self):
        item = SimpleNamespace(metadata={
            'review_bounce_pr_heads': {ORIGIN_PR['url']: HEAD_H},
            'review_bounce_same_head_count': 3,
            'review_finding_responses': {
                'findings': [{'id': 'P1', 'response': 'x', 'evidence': 'y'}]
            },
        })
        new_pr = dict(ORIGIN_PR, head_sha=HEAD_H2)
        with patch.object(
            WorkflowService,
            '_linked_pr_entries',
            return_value=[new_pr],
        ):
            WorkflowService._stamp_review_bounce(item)
        self.assertEqual(item.metadata.get('review_bounce_same_head_count'), 1)
        self.assertNotIn('review_finding_responses', item.metadata)
        self.assertEqual(
            item.metadata.get('review_bounce_pr_heads', {}).get(ORIGIN_PR['url']),
            HEAD_H2,
        )

    def test_finding_responses_required_shape(self):
        item = SimpleNamespace(metadata={})
        ok, detail = WorkflowService._review_finding_responses_complete(item)
        self.assertFalse(ok)
        item.metadata['review_finding_responses'] = {
            'findings': [{'id': 'P1-1', 'response': 'fixed', 'evidence': 'sha'}]
        }
        ok, detail = WorkflowService._review_finding_responses_complete(item)
        self.assertTrue(ok)

    def test_finding_responses_incomplete_without_evidence(self):
        item = SimpleNamespace(metadata={
            'review_finding_responses': {
                'findings': [{'id': 'P1-1', 'response': 'fixed', 'evidence': ''}]
            }
        })
        ok, detail = WorkflowService._review_finding_responses_complete(item)
        self.assertFalse(ok)
        self.assertIn('P1-1', detail)

    def test_pls675_production_order_unchanged_head_blocked_after_two_bounces(self):
        """PLS-675 escape: 2+ RC at H + unchanged rebuttal → fail closed."""
        url = ORIGIN_PR['url']
        item = SimpleNamespace(
            item_key='PLS-675',
            metadata={
                'review_bounce_at': '2026-08-01T00:00:00+00:00',
                'review_bounce_pr_heads': {url: HEAD_H},
                'review_bounce_same_head_count': 2,
                # No structured finding responses — the production failure mode.
            },
            pk=1,
        )
        with (
            patch.object(
                WorkflowService,
                '_refresh_review_reentry_pr_metadata',
                return_value=(False, {url: True}),
            ),
            patch.object(
                WorkflowService,
                '_current_linked_pr_heads',
                return_value={url: HEAD_H},
            ),
            patch.object(
                WorkflowService,
                '_active_linked_pr_entries',
                return_value=[dict(ORIGIN_PR)],
            ),
            patch.object(
                WorkflowService,
                '_linked_pr_entries',
                return_value=[dict(ORIGIN_PR)],
            ),
            patch.object(
                WorkflowService,
                '_has_review_rebuttal_since_bounce',
                return_value=True,  # author posted a PLAT-202 rebuttal
            ),
            patch.object(
                WorkflowService,
                '_review_reentry_retired_mirror_keys',
                return_value=set(),
            ),
        ):
            allowed, detail = WorkflowService._review_reentry_has_progress(item)
        self.assertFalse(allowed)
        self.assertIsNotNone(detail)
        self.assertIn('PLS-510', detail)
        self.assertRegex(detail, r'(finding|rebuttal)', )

    def test_new_head_with_finding_responses_is_admissible(self):
        """After head advances to H2, same-head breaker does not block."""
        url = ORIGIN_PR['url']
        item = SimpleNamespace(
            item_key='PLS-675',
            metadata={
                'review_bounce_at': '2026-08-01T00:00:00+00:00',
                'review_bounce_pr_heads': {url: HEAD_H},
                'review_bounce_same_head_count': 2,
                'review_finding_responses': {
                    'findings': [
                        {'id': 'P1', 'response': 'addressed', 'evidence': HEAD_H2[:12]}
                    ]
                },
            },
            pk=1,
        )
        advanced_pr = dict(ORIGIN_PR, head_sha=HEAD_H2)
        with (
            patch.object(
                WorkflowService,
                '_refresh_review_reentry_pr_metadata',
                return_value=(True, {url: True}),
            ),
            patch.object(
                WorkflowService,
                '_current_linked_pr_heads',
                return_value={url: HEAD_H2},
            ),
            patch.object(
                WorkflowService,
                '_active_linked_pr_entries',
                return_value=[advanced_pr],
            ),
            patch.object(
                WorkflowService,
                '_linked_pr_entries',
                return_value=[advanced_pr],
            ),
            patch.object(
                WorkflowService,
                '_review_reentry_retired_mirror_keys',
                return_value=set(),
            ),
        ):
            allowed, detail = WorkflowService._review_reentry_has_progress(item)
        self.assertTrue(allowed, msg=detail)

    # PLAT-11119: the breaker must not demand finding-responses when the live
    # provider shows ZERO REQUEST_CHANGES verdicts at the current head — the
    # counter was stamped by workflow transitions, not by real review verdicts
    # (approved+green, zero-findings PR must route).

    def test_request_changes_count_exact_head_only(self):
        head = HEAD_H
        rows = [
            {'state': 'REQUEST_CHANGES', 'commit_id': head},   # counts
            {'state': 'CHANGES_REQUESTED', 'commit_id': head}, # legacy spelling, counts
            {'state': 'REQUEST_CHANGES', 'commit_id': HEAD_H2},  # stale head, ignored
            {'state': 'REQUEST_CHANGES'},                      # missing commit_id, ignored
            {'state': 'APPROVED', 'commit_id': head},          # not a negative verdict
            {'commit_id': head},                               # no state, ignored
            'not-a-dict',                                      # malformed, ignored
        ]
        self.assertEqual(
            WorkflowService._count_forgejo_request_changes(rows, head), 2,
        )
        self.assertEqual(
            WorkflowService._count_forgejo_request_changes(None, head), 0,
        )
        self.assertEqual(
            WorkflowService._count_forgejo_request_changes(rows, ''), 0,
        )

    def _churn_item_with_counter(self, url):
        return SimpleNamespace(
            item_key='PLAT-11119',
            metadata={
                'review_bounce_at': '2026-10-03T00:00:00+00:00',
                'review_bounce_pr_heads': {url: HEAD_H},
                'review_bounce_same_head_count': 2,
                # No structured finding responses — the phantom wedge shape.
            },
            pk=1,
        )

    def _churn_context(self, url):
        return (
            patch.object(
                WorkflowService,
                '_refresh_review_reentry_pr_metadata',
                return_value=(False, {url: True}),
            ),
            patch.object(
                WorkflowService,
                '_current_linked_pr_heads',
                return_value={url: HEAD_H},
            ),
            patch.object(
                WorkflowService,
                '_active_linked_pr_entries',
                return_value=[dict(ORIGIN_PR)],
            ),
            patch.object(
                WorkflowService,
                '_linked_pr_entries',
                return_value=[dict(ORIGIN_PR)],
            ),
            patch.object(
                WorkflowService,
                '_has_review_rebuttal_since_bounce',
                return_value=True,
            ),
            patch.object(
                WorkflowService,
                '_review_reentry_retired_mirror_keys',
                return_value=set(),
            ),
        )

    def test_breaker_not_enforced_when_live_reviews_show_zero_request_changes(self):
        """PLAT-11119: zero live REQUEST_CHANGES at head → phantom counter → route."""
        url = ORIGIN_PR['url']
        item = self._churn_item_with_counter(url)
        with ExitStack() as stack:
            for cm in self._churn_context(url):
                stack.enter_context(cm)
            stack.enter_context(patch.object(
                WorkflowService,
                '_forgejo_request_changes_at_head',
                return_value=(0, None),
            ))
            allowed, detail = WorkflowService._review_reentry_has_progress(item)
        self.assertTrue(allowed, msg=detail)

    def test_breaker_enforced_when_live_reviews_corroborate_request_changes(self):
        """Live REQUEST_CHANGES verdicts at head → breaker binds (PLS-675 shape)."""
        url = ORIGIN_PR['url']
        item = self._churn_item_with_counter(url)
        with ExitStack() as stack:
            for cm in self._churn_context(url):
                stack.enter_context(cm)
            stack.enter_context(patch.object(
                WorkflowService,
                '_forgejo_request_changes_at_head',
                return_value=(2, None),
            ))
            allowed, detail = WorkflowService._review_reentry_has_progress(item)
        self.assertFalse(allowed)
        self.assertIsNotNone(detail)
        self.assertIn('PLS-510', detail)

    def test_breaker_kept_when_provider_lookup_errors(self):
        """Provider error cannot relax the breaker (fail-closed corroboration)."""
        url = ORIGIN_PR['url']
        item = self._churn_item_with_counter(url)
        with ExitStack() as stack:
            for cm in self._churn_context(url):
                stack.enter_context(cm)
            stack.enter_context(patch.object(
                WorkflowService,
                '_forgejo_request_changes_at_head',
                return_value=(0, 'forgejo_token_missing'),
            ))
            allowed, detail = WorkflowService._review_reentry_has_progress(item)
        self.assertFalse(allowed)
        self.assertIsNotNone(detail)
        self.assertIn('PLS-510', detail)
