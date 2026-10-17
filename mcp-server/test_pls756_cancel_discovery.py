"""PLS-756 MCP discovery regression: Cancel/Discard's required reason_category
contract (and exact enum) must be rendered by pulse_get_available_transitions,
with the execute payload shape shown, so an MCP caller can cancel correctly
without frontend knowledge."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")
os.environ.setdefault("PULSE_SUBSTRATE_CAP_MODE", "shadow")

import pulse_server  # noqa: E402


CANCEL_SCREEN = [
    {'field': 'comment', 'label': 'Why?', 'required': True},
    {'field': 'reason_category', 'label': 'Reason category', 'required': True,
     'enum': ['duplicate', 'invalid', 'obsolete', 'test-artifact']},
]


def _cancel_transition(**overrides):
    t = {
        'id': 'cancel-1',
        'name': 'Cancel / Discard',
        'to_status': 'cancelled',
        'to_status_label': 'Cancelled',
        'screen_fields': CANCEL_SCREEN,
        'description': 'Terminal discard path.',
        'knowledge_articles': [],
    }
    t.update(overrides)
    return t


class CancelDiscoveryRenderingTests(unittest.TestCase):

    def test_cancel_discovery_reports_reason_category_with_enum(self):
        with patch.object(pulse_server, 'api_request', return_value={
            'current_status': 'open',
            'transitions': [_cancel_transition()],
        }):
            rendered = pulse_server.get_available_transitions('PLS-756')

        self.assertIn('reason_category (duplicate|invalid|obsolete|test-artifact)', rendered)
        self.assertIn('comment', rendered)
        self.assertIn('screen_data=', rendered)
        self.assertIn('"reason_category": "<value>"', rendered)

    def test_cancel_discovery_payload_example_lists_all_required_fields(self):
        with patch.object(pulse_server, 'api_request', return_value={
            'current_status': 'open',
            'transitions': [_cancel_transition()],
        }):
            rendered = pulse_server.get_available_transitions('PLS-756')

        self.assertIn('Execute payload (required fields)', rendered)
        self.assertIn('"comment": "<value>"', rendered)

    def test_transition_without_enum_renders_field_name_only(self):
        t = _cancel_transition(
            screen_fields=[{'field': 'comment', 'label': 'Why?', 'required': True}])
        with patch.object(pulse_server, 'api_request', return_value={
            'current_status': 'open',
            'transitions': [t],
        }):
            rendered = pulse_server.get_available_transitions('PLS-756-0')

        self.assertIn('[requires: comment]', rendered)
        self.assertIn('"comment": "<value>"', rendered)

    def test_execute_guidance_documents_cancel_payload_shape(self):
        doc = pulse_server.execute_transition.__doc__ or ''
        self.assertIn('duplicate|invalid|obsolete|test-artifact', doc)
        self.assertIn('reason_category', doc)


if __name__ == '__main__':
    unittest.main()
