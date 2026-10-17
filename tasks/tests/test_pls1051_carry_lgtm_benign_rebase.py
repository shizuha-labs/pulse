"""PLS-1051: carry APPROVED reviews across a benign update-from-base.

An APPROVED review anchored to SHA N must survive a no-conflict rebase/merge
of the base branch (head advances, reviewed diff unchanged) without forcing a
full re-review cycle.  A substantive change to the reviewed lines changes the
diff-hash and still requires re-review.
"""

import os
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService

HEAD = 'a' * 40
STALE = 'b' * 40


class CarryApprovedAcrossBenignRebaseTests(SimpleTestCase):
    """Pure helper: carry APPROVED reviews only when the diff-hash matches."""

    def _diff_map(self, mapping):
        return lambda commit_id: mapping.get(commit_id)

    def test_carries_approved_when_diff_hash_matches(self):
        reviews = [
            {'user': {'login': 'rui'}, 'commit_id': STALE, 'state': 'APPROVED'},
            {'user': {'login': 'aoi'}, 'commit_id': HEAD, 'state': 'APPROVED'},
        ]
        hashes = {HEAD: 'hash-x', STALE: 'hash-x'}
        got = WorkflowService._carry_approved_reviews_across_benign_rebase(
            reviews, HEAD, self._diff_map(hashes),
        )
        self.assertEqual(got, {'rui'})

    def test_does_not_carry_when_diff_hash_differs(self):
        reviews = [
            {'user': {'login': 'rui'}, 'commit_id': STALE, 'state': 'APPROVED'},
        ]
        hashes = {HEAD: 'hash-x', STALE: 'hash-y'}
        got = WorkflowService._carry_approved_reviews_across_benign_rebase(
            reviews, HEAD, self._diff_map(hashes),
        )
        self.assertEqual(got, set())

    def test_never_carries_changes_requested_or_commented(self):
        reviews = [
            {'user': {'login': 'revi'}, 'commit_id': STALE, 'state': 'CHANGES_REQUESTED'},
            {'user': {'login': 'haru'}, 'commit_id': STALE, 'state': 'COMMENTED'},
        ]
        hashes = {HEAD: 'hash-x', STALE: 'hash-x'}
        got = WorkflowService._carry_approved_reviews_across_benign_rebase(
            reviews, HEAD, self._diff_map(hashes),
        )
        self.assertEqual(got, set())

    def test_fails_closed_when_current_hash_unknown(self):
        reviews = [
            {'user': {'login': 'rui'}, 'commit_id': STALE, 'state': 'APPROVED'},
        ]
        hashes = {STALE: 'hash-x'}
        got = WorkflowService._carry_approved_reviews_across_benign_rebase(
            reviews, HEAD, self._diff_map(hashes),
        )
        self.assertEqual(got, set())

    def test_fails_closed_when_review_commit_hash_unknown(self):
        reviews = [
            {'user': {'login': 'rui'}, 'commit_id': STALE, 'state': 'APPROVED'},
        ]
        hashes = {HEAD: 'hash-x'}
        got = WorkflowService._carry_approved_reviews_across_benign_rebase(
            reviews, HEAD, self._diff_map(hashes),
        )
        self.assertEqual(got, set())

    def test_empty_head_yields_no_carry(self):
        reviews = [
            {'user': {'login': 'rui'}, 'commit_id': STALE, 'state': 'APPROVED'},
        ]
        got = WorkflowService._carry_approved_reviews_across_benign_rebase(
            reviews, '', self._diff_map({}),
        )
        self.assertEqual(got, set())

    def test_missing_or_blank_commit_id_never_carries(self):
        reviews = [
            {'user': {'login': 'missing'}, 'state': 'APPROVED'},
            {'user': {'login': 'blank'}, 'commit_id': '', 'state': 'APPROVED'},
        ]
        hashes = {HEAD: 'hash-x'}
        got = WorkflowService._carry_approved_reviews_across_benign_rebase(
            reviews, HEAD, self._diff_map(hashes),
        )
        self.assertEqual(got, set())


class ForgejoPrDiffHashTests(SimpleTestCase):
    """Diff-hash is stable for identical feature diffs, fail-closed otherwise."""

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_stable_hash_from_compare_payload(self, request_get, _cache_get):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            'files': [
                {'filename': 'b.txt', 'status': 'modified', 'patch': '@@ -1 +1 @@\n-x\n+y'},
                {'filename': 'a.txt', 'status': 'added', 'patch': '+new'},
            ],
        }
        request_get.return_value = response
        h1 = WorkflowService._forgejo_pr_diff_hash(
            'shizuha-labs', 'pulse', 42, 'main', HEAD,
            token='t', origin_url='https://origin.shizuha.com',
        )
        h2 = WorkflowService._forgejo_pr_diff_hash(
            'shizuha-labs', 'pulse', 42, 'main', HEAD,
            token='t', origin_url='https://origin.shizuha.com',
        )
        self.assertEqual(h1, h2)
        self.assertIsNotNone(h1)
        self.assertEqual(
            request_get.call_args_list[0].args[0],
            'https://origin.shizuha.com/api/v1/repos/shizuha-labs/pulse/'
            'compare/main...' + HEAD,
        )

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_fails_closed_on_provider_error(self, request_get, _cache_get):
        response = Mock()
        response.raise_for_status.side_effect = RuntimeError('boom')
        request_get.return_value = response
        got = WorkflowService._forgejo_pr_diff_hash(
            'shizuha-labs', 'pulse', 42, 'main', HEAD,
            token='t', origin_url='https://origin.shizuha.com',
        )
        self.assertIsNone(got)

    def test_fails_closed_on_missing_inputs(self):
        for kwargs in (
            {'head_sha': '', 'base_ref': 'main'},
            {'head_sha': HEAD, 'base_ref': ''},
            {'head_sha': HEAD, 'base_ref': 'main', 'token': ''},
            {'head_sha': HEAD, 'base_ref': 'main', 'origin_url': ''},
        ):
            got = WorkflowService._forgejo_pr_diff_hash(
                'shizuha-labs', 'pulse', 42, kwargs['base_ref'],
                kwargs['head_sha'], token=kwargs.get('token', 't'),
                origin_url=kwargs.get('origin_url', 'https://origin.shizuha.com'),
            )
            self.assertIsNone(got)


class ForgejoCurrentHeadReviewersCarryTests(SimpleTestCase):
    """End-to-end: stale APPROVED review carries across a benign rebase."""

    canonical_url = 'https://origin.shizuha.com/shizuha-labs/pulse/pulls/42'

    def _entry(self):
        return {'url': self.canonical_url, 'head_sha': HEAD}

    def _pr_payload(self):
        return {'head': {'sha': HEAD}, 'base': {'ref': 'main'}}

    def _reviews(self, stale_state='APPROVED'):
        return [
            {'user': {'login': 'rui'}, 'commit_id': STALE, 'state': stale_state},
        ]

    def _compare_payload(self, filename='feature.py', patch='+new'):
        return {'files': [{'filename': filename, 'status': 'modified', 'patch': patch}]}

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_benign_rebase_carries_stale_approved(self, request_get, _cache_get):
        pr = Mock(); pr.raise_for_status.return_value = None; pr.json.return_value = self._pr_payload()
        rv = Mock(); rv.raise_for_status.return_value = None; rv.json.return_value = self._reviews()
        cmp = Mock(); cmp.raise_for_status.return_value = None; cmp.json.return_value = self._compare_payload()
        request_get.side_effect = [pr, rv, cmp, cmp]
        with patch.dict(
            os.environ,
            {'FORGEJO_TOKEN': 'test-token', 'ORIGIN_URL': 'https://origin.shizuha.com'},
            clear=True,
        ):
            reviewers, error = WorkflowService._forgejo_current_head_reviewers(self._entry())
        self.assertEqual(reviewers, {'rui'})
        self.assertIsNone(error)
        # PR + reviews + compare@HEAD + compare@STALE
        self.assertEqual(request_get.call_count, 4)

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_substantive_change_does_not_carry(self, request_get, _cache_get):
        pr = Mock(); pr.raise_for_status.return_value = None; pr.json.return_value = self._pr_payload()
        rv = Mock(); rv.raise_for_status.return_value = None; rv.json.return_value = self._reviews()
        cmp_head = Mock(); cmp_head.raise_for_status.return_value = None
        cmp_head.json.return_value = self._compare_payload(patch='+new')
        cmp_stale = Mock(); cmp_stale.raise_for_status.return_value = None
        cmp_stale.json.return_value = self._compare_payload(patch='+old')
        request_get.side_effect = [pr, rv, cmp_head, cmp_stale]
        with patch.dict(
            os.environ,
            {'FORGEJO_TOKEN': 'test-token', 'ORIGIN_URL': 'https://origin.shizuha.com'},
            clear=True,
        ):
            reviewers, error = WorkflowService._forgejo_current_head_reviewers(self._entry())
        self.assertEqual(reviewers, set())
        self.assertIsNone(error)

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_changes_requested_never_carries(self, request_get, _cache_get):
        pr = Mock(); pr.raise_for_status.return_value = None; pr.json.return_value = self._pr_payload()
        rv = Mock(); rv.raise_for_status.return_value = None
        rv.json.return_value = self._reviews(stale_state='CHANGES_REQUESTED')
        cmp = Mock(); cmp.raise_for_status.return_value = None; cmp.json.return_value = self._compare_payload()
        request_get.side_effect = [pr, rv, cmp, cmp]
        with patch.dict(
            os.environ,
            {'FORGEJO_TOKEN': 'test-token', 'ORIGIN_URL': 'https://origin.shizuha.com'},
            clear=True,
        ):
            reviewers, error = WorkflowService._forgejo_current_head_reviewers(self._entry())
        self.assertEqual(reviewers, set())
        self.assertIsNone(error)

    @patch('tasks.services.workflow_service.cache.get', return_value=None)
    @patch('requests.get')
    def test_no_stale_approved_skips_diff_hash_calls(self, request_get, _cache_get):
        pr = Mock(); pr.raise_for_status.return_value = None; pr.json.return_value = self._pr_payload()
        rv = Mock(); rv.raise_for_status.return_value = None
        rv.json.return_value = [
            {'user': {'login': 'rui'}, 'commit_id': HEAD, 'state': 'APPROVED'},
        ]
        request_get.side_effect = [pr, rv]
        with patch.dict(
            os.environ,
            {'FORGEJO_TOKEN': 'test-token', 'ORIGIN_URL': 'https://origin.shizuha.com'},
            clear=True,
        ):
            reviewers, error = WorkflowService._forgejo_current_head_reviewers(self._entry())
        self.assertEqual(reviewers, {'rui'})
        self.assertIsNone(error)
        self.assertEqual(request_get.call_count, 2)
