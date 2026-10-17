"""PLS-792 MCP discovery regression: Run Now's screen_fields are keyed by
``key`` (not ``field``/``label``). pulse_get_available_transitions must render
the required ``expected_cycle_revision`` (and optional CAS siblings) so an MCP
caller can refresh -> execute without private implementation knowledge."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")
os.environ.setdefault("PULSE_SUBSTRATE_CAP_MODE", "shadow")

import pulse_server  # noqa: E402


RUN_NOW_SCREEN = [
    {'key': 'expected_cycle_revision', 'type': 'integer', 'required': True},
    {'key': 'expected_generation', 'type': 'integer', 'required': False},
    {'key': 'expected_due_at', 'type': 'string', 'required': False},
    {'key': 'expected_completion_watermark', 'type': 'string', 'required': False},
    {'key': 'recurring_event_id', 'type': 'string', 'required': False},
]


def _run_now_transition(**overrides):
    t = {
        'id': 'run-now-1',
        'name': 'Run Now',
        'to_status': 'todo',
        'to_status_label': 'To Do',
        'screen_fields': RUN_NOW_SCREEN,
        'description': 'Manually start the next recurring cycle now.',
        'knowledge_articles': [],
    }
    t.update(overrides)
    return t


class RunNowKeyScreenFieldRenderingTests(unittest.TestCase):

    def test_run_now_discovery_exposes_expected_cycle_revision_by_key(self):
        """key-based screen_fields must render named fields, not '?'."""
        with patch.object(pulse_server, 'api_request', return_value={
            'current_status': 'scheduled',
            'transitions': [_run_now_transition()],
        }):
            rendered = pulse_server.get_available_transitions('PLS-792')

        self.assertIn('expected_cycle_revision', rendered)
        self.assertNotIn('[requires: ?, ?, ?, ?, ?]', rendered)
        self.assertIn('"expected_cycle_revision": "<value>"', rendered)

    def test_run_now_payload_lists_required_revision_only(self):
        """Only the required field appears in the execute payload example."""
        with patch.object(pulse_server, 'api_request', return_value={
            'current_status': 'scheduled',
            'transitions': [_run_now_transition()],
        }):
            rendered = pulse_server.get_available_transitions('PLS-792-0')

        self.assertIn('Execute payload (required fields)', rendered)
        self.assertIn('"expected_cycle_revision": "<value>"', rendered)
        # Optional CAS siblings must NOT be required in the payload example.
        self.assertNotIn('"expected_generation": "<value>"', rendered)

    def test_run_now_requires_line_lists_all_cas_fields(self):
        with patch.object(pulse_server, 'api_request', return_value={
            'current_status': 'scheduled',
            'transitions': [_run_now_transition()],
        }):
            rendered = pulse_server.get_available_transitions('PLS-792-1')

        self.assertIn('expected_cycle_revision', rendered)
        self.assertIn('expected_generation', rendered)
        self.assertIn('expected_due_at', rendered)
        self.assertIn('expected_completion_watermark', rendered)
        self.assertIn('recurring_event_id', rendered)

    def test_mixed_field_and_key_screen_fields_both_render(self):
        """A transition mixing field- and key-based descriptors renders all names."""
        t = _run_now_transition(screen_fields=[
            {'field': 'comment', 'label': 'Why?', 'required': True},
            {'key': 'expected_cycle_revision', 'type': 'integer', 'required': True},
        ])
        with patch.object(pulse_server, 'api_request', return_value={
            'current_status': 'scheduled',
            'transitions': [t],
        }):
            rendered = pulse_server.get_available_transitions('PLS-792-2')

        self.assertIn('comment', rendered)
        self.assertIn('expected_cycle_revision', rendered)
        self.assertNotIn('?', rendered.split('[requires:')[1].split(']')[0])


if __name__ == '__main__':
    unittest.main()
