"""Reviewer-pass consumes complete provider state, never absence of metadata."""
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError

HEAD = 'ca96339467960dd7651c7da9f83eb7ae3c65bf63'
URL = 'https://origin.shizuha.com/shizuha-labs/pulse/pulls/770'
BASE = 'https://origin.example.test/api/v1/repos/shizuha-labs/pulse/pulls/770'


def response(body):
    result = Mock(status_code=200)
    result.json.return_value = body
    result.raise_for_status.return_value = None
    return result


def review(number=1, state='APPROVED', **extra):
    return dict(id=number, state=state, commit_id=HEAD, dismissed=False, **extra)


class ReviewerRequestSnapshotTests(SimpleTestCase):
    def setUp(self):
        self.item = SimpleNamespace(metadata={
            'pull_requests': [{'url': URL, 'head_sha': HEAD, 'status': 'open'}],
            'review_bounce_pr_heads': {URL: HEAD},
        }, save=Mock())
        self.transition = SimpleNamespace(validators=[{
            'type': 'reviewer_pass_requires_disposition'}], name='Review Passed', id='pass')
        self.env = patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'synthetic-test-token', 'ORIGIN_URL': 'https://origin.example.test'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_gate(self, payloads, comment='PLAT-10330 disposition: all open change-requests'):
        with patch('requests.get', side_effect=[response(p) if not isinstance(p, Exception) else p
                                               for p in payloads]) as get:
            WorkflowService._run_validators(self.transition, self.item,
                                           screen_data={'comment': comment})
        return get

    def head(self, sha=HEAD):
        return {'head': {'sha': sha}}

    def test_actual_same_head_two_approvals_produce_known_empty(self):
        get = self.run_gate([self.head(), [review(6568), review(6673)], [], self.head()])
        self.assertEqual(self.item.metadata['open_change_request_ids'], [])
        self.assertEqual(get.call_args_list[1].args[0], BASE + '/reviews')
        self.item.save.assert_called_once_with(update_fields=['metadata'])

    def test_missing_metadata_and_failed_read_remain_unknown(self):
        with self.assertRaises(WorkflowValidationError):
            self.run_gate([self.head(), RuntimeError('synthetic unavailable')])
        self.assertNotIn('open_change_request_ids', self.item.metadata)

    def test_changed_head_during_read_refuses(self):
        with self.assertRaises(WorkflowValidationError):
            self.run_gate([self.head(), [], self.head('b' * 40)])

    def test_paginated_negative_not_hidden_behind_first_hundred(self):
        first = [review(n) for n in range(1, 101)]
        with self.assertRaises(WorkflowValidationError):
            self.run_gate([self.head(), first, [review(101, 'REQUEST_CHANGES')], [], self.head()],
                          'PLAT-10330 disposition: no open requests')
        self.assertEqual(self.item.metadata['open_change_request_ids'], [URL + '#review-101'])

    def test_unknown_review_shapes_refuse_known_empty(self):
        for bad in ({}, [None], [review(state='UNRECOGNIZED')],
                    [dict(id=1, state='REQUEST_CHANGES')],
                    [review(number=True)], [review() , review()]):
            with self.subTest(shape=bad), self.assertRaises(WorkflowValidationError):
                self.run_gate([self.head(), bad, self.head()])

    def test_known_empty_still_requires_disposition_marker(self):
        with self.assertRaises(WorkflowValidationError):
            self.run_gate([self.head(), [], self.head()], 'LGTM')

    def test_stale_negative_and_explicitly_dismissed_are_not_open(self):
        stale = review(1, 'REQUEST_CHANGES'); stale['commit_id'] = 'b' * 40
        dismissed = review(2, 'REQUEST_CHANGES'); dismissed['dismissed'] = True
        self.run_gate([self.head(), [stale, dismissed], [], self.head()])
        self.assertEqual(self.item.metadata['open_change_request_ids'], [])

    def test_failed_refresh_does_not_reuse_prior_known_empty(self):
        self.run_gate([self.head(), [], self.head()])
        with self.assertRaises(WorkflowValidationError):
            self.run_gate([self.head(), RuntimeError('unavailable')])

    def test_provider_head_mismatch_never_reads_reviews(self):
        with self.assertRaises(WorkflowValidationError):
            self.run_gate([self.head('b' * 40)])

    def test_untrusted_link_cannot_select_authenticated_destination(self):
        bad = 'https://attacker.example/pulls/770'
        self.item.metadata['pull_requests'][0]['url'] = bad
        self.item.metadata['review_bounce_pr_heads'] = {bad: HEAD}
        with patch('requests.get') as get, self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(self.transition, self.item,
                screen_data={'comment': 'PLAT-10330 disposition: all open change-requests'})
        get.assert_not_called()

    def test_missing_provider_configuration_is_unknown(self):
        with patch.dict(os.environ, {}, clear=True), patch('requests.get') as get:
            with self.assertRaises(WorkflowValidationError):
                WorkflowService._run_validators(self.transition, self.item,
                    screen_data={'comment': 'PLAT-10330 disposition: all open change-requests'})
        get.assert_not_called()

    def test_page_failure_cannot_turn_first_page_approvals_into_empty(self):
        with self.assertRaises(WorkflowValidationError):
            self.run_gate([self.head(), [review(n) for n in range(1, 101)],
                           RuntimeError('second page unavailable')])
        self.item.save.assert_not_called()

    def test_server_clamped_short_pages_are_not_complete(self):
        with self.assertRaises(WorkflowValidationError):
            self.run_gate([self.head(), [review(1)], [review(2, 'REQUEST_CHANGES')],
                           [], self.head()], 'PLAT-10330 disposition: no requests')
        self.assertEqual(self.item.metadata['open_change_request_ids'], [URL + '#review-2'])

    def test_legacy_blank_ids_remain_unparseable(self):
        self.item.metadata['open_change_request_ids'] = [' ', '']
        ok, detail = WorkflowService._reviewer_pass_dispositions_open_requests(
            self.item, {'comment': 'PLAT-10330 disposition: all open change-requests'})
        self.assertFalse(ok)
        self.assertIn('unparseable', detail)

    def test_malformed_review_commit_is_unknown_not_stale(self):
        for commit in (True, 7, 'invalid', 'a' * 39):
            bad = review(1, 'REQUEST_CHANGES'); bad['commit_id'] = commit
            with self.subTest(commit=commit), self.assertRaises(WorkflowValidationError):
                self.run_gate([self.head(), [bad], [], self.head()])

    def test_redirected_json_is_not_provider_authority(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        head = self.head()
        class Other(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps([] if '/reviews' in self.path else head).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        other = ThreadingHTTPServer(('127.0.0.1', 0), Other)
        class Trusted(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{other.server_port}{self.path}')
                self.end_headers()
            def log_message(self, *args): pass
        trusted = ThreadingHTTPServer(('127.0.0.1', 0), Trusted)
        for server in (other, trusted):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.dict(os.environ, {'ORIGIN_URL': f'http://127.0.0.1:{trusted.server_port}'}):
                with self.assertRaises(WorkflowValidationError):
                    WorkflowService._run_validators(self.transition, self.item,
                        screen_data={'comment': 'PLAT-10330 disposition: all open change-requests'})
            self.assertNotIn('open_change_request_ids', self.item.metadata)
        finally:
            for server in (trusted, other):
                server.shutdown(); server.server_close()

    def test_non_200_json_is_not_successful_review_authority(self):
        for code in (201, 204, 301, 304, 401, 404, 500):
            first = response(self.head()); first.status_code = code
            with self.subTest(code=code), patch('requests.get', return_value=first):
                with self.assertRaises(WorkflowValidationError):
                    WorkflowService._run_validators(self.transition, self.item,
                        screen_data={'comment': 'PLAT-10330 disposition: all open change-requests'})
            first.json.assert_not_called()
