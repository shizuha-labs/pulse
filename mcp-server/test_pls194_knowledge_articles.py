"""PLS-194 MCP renderer regression tests."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name('pulse_server.py')
spec = importlib.util.spec_from_file_location('pulse_server_under_test', MODULE_PATH)
pulse_server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pulse_server
spec.loader.exec_module(pulse_server)


def test_get_available_transitions_skips_malformed_article_string():
    with patch.object(pulse_server, 'api_request', return_value={
        'current_status': 'awaiting_merge',
        'transitions': [{
            'id': 't1',
            'name': 'Merge',
            'to_status': 'done',
            'to_status_label': 'Done',
            'knowledge_articles': 'raw malformed string',
        }],
    }):
        rendered = pulse_server.get_available_transitions('P194-1')

    assert 'Merge' in rendered
    assert 'raw malformed string' not in rendered


def test_execute_transition_skips_malformed_executed_and_next_articles():
    with patch.object(pulse_server, 'api_request', return_value={
        'status': 'done',
        'title': 'P194 task',
        'executed_transition': {
            'name': 'Merge',
            'knowledge_articles': 'raw malformed string',
        },
        'next_transitions': [{
            'id': 't2',
            'name': 'Reopen',
            'to_status': 'awaiting_merge',
            'knowledge_articles': 'also malformed',
        }],
    }):
        rendered = pulse_server.execute_transition('P194-1', transition_id='t1')

    assert 'Transitioned' in rendered
    assert 'raw malformed string' not in rendered
    assert 'also malformed' not in rendered
