"""Gateway failed turns must make the owning agent unavailable to routing."""

import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from tasks.tasks import (
    _classify_connect_health_signal,
    _connect_health_bad_agents,
    _connect_provider_unavailable_agents,
)


def _response(payload):
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.read.return_value = json.dumps(payload).encode()
    return response


class GatewayFailedTurnAvailabilityTests(SimpleTestCase):
    @patch('tasks.net_utils.safe_urlopen')
    def test_current_gateway_error_ring_marks_agent_unavailable(self, urlopen):
        urlopen.return_value = _response({
            'agents': {
                'nagi': {
                    'agent_username': 'nagi',
                    'runtime': {'harness': 'shizuha-gateway'},
                    # Live regression: the old gateway incorrectly said ok=true
                    # while carrying ten current ECONNREFUSED turn failures.
                    'health': {
                        'ok': True,
                        'recent_errors': ['Cannot connect to vLLM: ECONNREFUSED'],
                    },
                },
            },
        })

        self.assertEqual(_connect_provider_unavailable_agents(), ['nagi@shizuha.com'])

    @patch('tasks.net_utils.safe_urlopen')
    def test_recovered_gateway_with_cleared_ring_is_available(self, urlopen):
        urlopen.return_value = _response({
            'agents': {
                'nagi': {
                    'agent_username': 'nagi',
                    'runtime': {'harness': 'shizuha-gateway'},
                    'health': {'ok': True, 'recent_errors': []},
                },
            },
        })

        self.assertEqual(_connect_provider_unavailable_agents(), [])

    @patch('tasks.net_utils.safe_urlopen')
    def test_historical_errors_on_other_bridge_do_not_false_demote(self, urlopen):
        urlopen.return_value = _response({
            'agents': {
                'hiro': {
                    'agent_username': 'hiro',
                    'runtime': {'harness': 'claude-code'},
                    'health': {
                        'ok': True,
                        'provider_unavailable': False,
                        'recent_errors': ['old recovered quota error'],
                    },
                },
            },
        })

        self.assertEqual(_connect_provider_unavailable_agents(), [])

    @patch('tasks.net_utils.safe_urlopen')
    def test_generic_gateway_error_is_not_mislabeled_provider_unavailable(self, urlopen):
        urlopen.return_value = _response({
            'agents': {
                'nagi': {
                    'agent_username': 'nagi',
                    'runtime': {'harness': 'shizuha-gateway'},
                    'health': {
                        'ok': False,
                        'provider_unavailable': False,
                        'recent_errors': ['Internal tool dispatch invariant failed'],
                    },
                },
            },
        })

        # Not a provider/token outage — must not land in the provider-only list.
        self.assertEqual(_connect_provider_unavailable_agents(), [])
        # But health.ok=false with an error ring is runtime_unhealthy (duration-
        # gated by the availability sweep before demotion).
        self.assertEqual(
            _connect_health_bad_agents(),
            {'nagi@shizuha.com': 'runtime_unhealthy'},
        )

    def test_agy_class_runtime_unhealthy_from_issue_types(self):
        reason = _classify_connect_health_signal(
            {
                'ok': False,
                'needs_help': True,
                'consecutive_errors': 10,
                'recent_errors': ['additionalProperties at tools[0]'],
                'health_summary': {
                    'issues': [{'type': 'runtime_unhealthy', 'severity': 'error'}],
                },
            },
            {'harness': 'gemini_cli_server'},
        )
        self.assertEqual(reason, 'runtime_unhealthy')
