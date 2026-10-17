"""PLS-1043: CI test-gate flake probe tests."""
from unittest import mock

from django.test import SimpleTestCase, TestCase

from tasks.services import ci_flake_probe
from tasks.services.ci_flake_probe import (
    FRONTEND_GATE_CONTEXT,
    TEST_GATE_CONTEXT,
    check_test_gate_flake,
    classify_commit_statuses,
)


def _status(ctx, state):
    return {'context': ctx, 'state': state}


class ClassifyCommitStatusesTests(SimpleTestCase):
    def test_flake_when_test_red_frontend_green(self):
        statuses = [
            _status(TEST_GATE_CONTEXT, 'failure'),
            _status(FRONTEND_GATE_CONTEXT, 'success'),
        ]
        self.assertEqual(classify_commit_statuses(statuses), 'flake')

    def test_code_when_test_red_frontend_red(self):
        statuses = [
            _status(TEST_GATE_CONTEXT, 'failure'),
            _status(FRONTEND_GATE_CONTEXT, 'failure'),
        ]
        self.assertEqual(classify_commit_statuses(statuses), 'code')

    def test_code_when_test_red_frontend_absent(self):
        statuses = [_status(TEST_GATE_CONTEXT, 'failure')]
        self.assertEqual(classify_commit_statuses(statuses), 'code')

    def test_ok_when_test_green(self):
        statuses = [
            _status(TEST_GATE_CONTEXT, 'success'),
            _status(FRONTEND_GATE_CONTEXT, 'success'),
        ]
        self.assertEqual(classify_commit_statuses(statuses), 'ok')

    def test_none_when_no_test_gate_status(self):
        statuses = [_status('some-other-check', 'success')]
        self.assertIsNone(classify_commit_statuses(statuses))

    def test_empty_statuses_returns_none(self):
        self.assertIsNone(classify_commit_statuses([]))


class CheckTestGateFlakeTests(TestCase):
    @mock.patch('tasks.services.ci_flake_probe._http_json')
    @mock.patch('tasks.services.ci_flake_probe._record_recurrence')
    @mock.patch('tasks.services.ci_flake_probe._fire_flake_alert')
    @mock.patch('tasks.services.ci_flake_probe._forgejo_token', return_value='tok')
    def test_fires_alert_when_flake_recurrence_passes_threshold(
        self, mock_token, mock_alert, mock_recur, mock_http,
    ):
        head = 'a' * 40
        mock_http.side_effect = [
            # pulls
            [{'number': 73, 'head': {'sha': head}}],
            # commit status for the head
            {'statuses': [
                _status(TEST_GATE_CONTEXT, 'failure'),
                _status(FRONTEND_GATE_CONTEXT, 'success'),
            ]},
        ]
        mock_recur.return_value = 3  # >= threshold
        result = check_test_gate_flake(threshold=3, window_hours=24)
        self.assertEqual(result['checked_heads'], 1)
        self.assertEqual(len(result['flakes']), 1)
        self.assertEqual(result['alerts'], 1)
        mock_alert.assert_called_once()
        args = mock_alert.call_args[0]
        self.assertEqual(args[0], head)
        self.assertEqual(args[1], 73)
        self.assertEqual(args[2], 3)

    @mock.patch('tasks.services.ci_flake_probe._http_json')
    @mock.patch('tasks.services.ci_flake_probe._record_recurrence')
    @mock.patch('tasks.services.ci_flake_probe._fire_flake_alert')
    @mock.patch('tasks.services.ci_flake_probe._forgejo_token', return_value='tok')
    def test_no_alert_below_threshold(
        self, mock_token, mock_alert, mock_recur, mock_http,
    ):
        head = 'b' * 40
        mock_http.side_effect = [
            [{'number': 74, 'head': {'sha': head}}],
            {'statuses': [
                _status(TEST_GATE_CONTEXT, 'failure'),
                _status(FRONTEND_GATE_CONTEXT, 'success'),
            ]},
        ]
        mock_recur.return_value = 1  # below threshold
        result = check_test_gate_flake(threshold=3, window_hours=24)
        self.assertEqual(result['checked_heads'], 1)
        self.assertEqual(len(result['flakes']), 1)
        self.assertEqual(result['alerts'], 0)
        mock_alert.assert_not_called()

    @mock.patch('tasks.services.ci_flake_probe._http_json')
    @mock.patch('tasks.services.ci_flake_probe._forgejo_token', return_value='tok')
    def test_code_failure_not_flagged_as_flake(self, mock_token, mock_http):
        head = 'c' * 40
        mock_http.side_effect = [
            [{'number': 75, 'head': {'sha': head}}],
            {'statuses': [
                _status(TEST_GATE_CONTEXT, 'failure'),
                _status(FRONTEND_GATE_CONTEXT, 'failure'),
            ]},
        ]
        result = check_test_gate_flake(threshold=3, window_hours=24)
        self.assertEqual(result['checked_heads'], 1)
        self.assertEqual(result['flakes'], [])
        self.assertEqual(result['alerts'], 0)

    @mock.patch('tasks.services.ci_flake_probe._forgejo_token', return_value='')
    def test_no_token_returns_early(self, mock_token):
        result = check_test_gate_flake()
        self.assertEqual(result['checked_heads'], 0)
        self.assertEqual(result['alerts'], 0)
