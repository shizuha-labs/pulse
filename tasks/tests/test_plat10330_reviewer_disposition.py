"""PLAT-10330: reviewer pass on an unchanged bounce head must disposition."""

from types import SimpleNamespace

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService, WorkflowValidationError

URL = 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/1566'
HEAD = 'abc123'


def _item(*, head=HEAD, bounce=HEAD, ids=None):
    meta = {
        'pull_requests': [{'url': URL, 'head_sha': head, 'status': 'open'}],
        'review_bounce_pr_heads': {URL: bounce},
    }
    if ids is not None:
        meta['open_change_request_ids'] = ids
    return SimpleNamespace(metadata=meta)


class ReviewerPassDispositionTests(SimpleTestCase):
    def test_unchanged_head_without_marker_fails_closed(self):
        ok, detail = WorkflowService._reviewer_pass_dispositions_open_requests(
            _item(ids=['P1-ni']), {'comment': 'LGTM'},
        )
        self.assertFalse(ok)
        self.assertIn('disposition', detail)

    def test_missing_ids_are_parse_ambiguity(self):
        ok, detail = WorkflowService._reviewer_pass_dispositions_open_requests(
            _item(ids=None),
            {'comment': 'PLAT-10330 disposition: all open change-requests'},
        )
        self.assertFalse(ok)
        self.assertIn('unparseable', detail)

    def test_naming_each_id_passes(self):
        ok, detail = WorkflowService._reviewer_pass_dispositions_open_requests(
            _item(ids=['P1-ni', 'P2-reika']),
            {'comment': 'PLAT-10330 disposition: P1-ni rebutted; P2-reika fixed in comment'},
        )
        self.assertTrue(ok)
        self.assertEqual(detail, '')

    def test_advanced_head_is_exempt(self):
        ok, _detail = WorkflowService._reviewer_pass_dispositions_open_requests(
            _item(head='newhead', bounce=HEAD, ids=['P1-ni']),
            {'comment': 'approve'},
        )
        self.assertTrue(ok)

    def test_validator_rejects_bare_pass(self):
        transition = SimpleNamespace(
            validators=[{'type': 'reviewer_pass_requires_disposition'}],
            name='Review Passed',
            id='plat-10330',
        )
        item = _item(ids=['P1-ni'])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(
                transition, item, screen_data={'comment': 'approve'},
            )
        self.assertIn('PLAT-10330', str(ctx.exception))

    def test_removed_retired_mirror_carrier_is_exempt(self):
        """PLS-421 carrier: the bounce entry's carrier row was purged from the
        card (retired github.com mirror identity, removal audited under
        removed_pull_requests) and the covering change moved to the canonical
        Origin row. The removed identity has no head to compare and can never
        advance — it must not wedge Review Passed forever."""
        gh = 'https://github.com/shizuha-labs/shizuha-tasks/pull/532'
        meta = {
            'pull_requests': [
                {'url': 'https://origin.shizuha.com/shizuha-labs/pulse/pulls/868',
                 'head_sha': 'cbc9c332', 'status': 'open'},
            ],
            'review_bounce_pr_heads': {gh: '0c8ec392'},
            'removed_pull_requests': [
                {'url': gh, 'reason': 'retired mirror identity', 'removed_at': '2026-10-22'},
            ],
            'open_change_request_ids': ['P1-ni'],
        }
        item = SimpleNamespace(metadata=meta)
        ok, detail = WorkflowService._reviewer_pass_dispositions_open_requests(
            item, {'comment': 'approve — covering change on the canonical Origin row'},
        )
        self.assertTrue(ok)
        self.assertEqual(detail, '')

    def test_stale_carrier_without_removal_audit_still_fails_closed(self):
        """A bounce entry with NO current linked row and NO removal audit is
        still parse ambiguity — the exemption requires the audited removal."""
        gh = 'https://github.com/shizuha-labs/shizuha-tasks/pull/532'
        meta = {
            'pull_requests': [
                {'url': 'https://origin.shizuha.com/shizuha-labs/pulse/pulls/868',
                 'head_sha': 'cbc9c332', 'status': 'open'},
            ],
            'review_bounce_pr_heads': {gh: '0c8ec392'},
            'open_change_request_ids': ['P1-ni'],
        }
        item = SimpleNamespace(metadata=meta)
        ok, detail = WorkflowService._reviewer_pass_dispositions_open_requests(
            item, {'comment': 'approve'},
        )
        self.assertFalse(ok)
        self.assertIn('lacks bounce or current head provenance', detail)
