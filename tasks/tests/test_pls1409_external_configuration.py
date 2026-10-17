"""External installation configuration must preserve the URL trust boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys

from django.test import SimpleTestCase


class ExternalConfigurationTests(SimpleTestCase):
    def run_isolated(self, source, **configuration):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('ORIGIN_', 'SHIZUHA_EXPECTED_', 'PULSE_FLEET_READ_', 'PULSE_FROZEN_'))}
        env.update(configuration)
        result = subprocess.run(
            [sys.executable, '-c', source], cwd=Path(__file__).resolve().parents[2],
            env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_custom_origin_is_the_only_canonical_pr_authority(self):
        result = self.run_isolated('''
import json
from tasks.origin_provenance import parse_origin_pr_url, canonical_origin_pr_url, canonical_origin_run_url, origin_ci_logs_url
base = 'https://code.example.test'
invalid = [
 'https://code.example.test.evil.test/o/r/pulls/7',
 'https://origin.shizuha.com/o/r/pulls/7',
 'http://code.example.test/o/r/pulls/7',
 'https://code.example.test:443/o/r/pulls/7',
 'https://user@code.example.test/o/r/pulls/7',
 base + '/o/r/pulls/7?token=bad', base + '/o/r/pulls/7#bad',
 base + '/o%2Fr/pulls/7', base + '/o/r/pull/7',
]
print(json.dumps({
 'coordinates': parse_origin_pr_url(base + '/o/r/pulls/7'),
 'canonical': canonical_origin_pr_url('o', 'r', 7),
 'run': canonical_origin_run_url('http://forgejo.internal/o/r/actions/runs/4'),
 'invalid': [parse_origin_pr_url(value) for value in invalid],
 'logs': origin_ci_logs_url({'repo':'o/r','run_number':'4'}),
}))
''', ORIGIN_PUBLIC_URL='https://code.example.test',
            ORIGIN_INTERNAL_HOST_ALIASES='forgejo.internal',
            ORIGIN_CI_LOGS_URL='https://logs.example.test/logs')
        self.assertEqual(result['coordinates'], ['o', 'r', 7])
        self.assertEqual(result['canonical'], 'https://code.example.test/o/r/pulls/7')
        self.assertEqual(result['run'], 'https://code.example.test/o/r/actions/runs/4')
        self.assertEqual(result['invalid'], [None] * 9)
        self.assertEqual(result['logs'], 'https://logs.example.test/logs?repo=o%2Fr&run=4')

    def test_unconfigured_origin_never_points_at_first_party(self):
        result = self.run_isolated('''
import json
from tasks.origin_provenance import canonical_origin_pr_url, origin_ci_logs_url, development_repo_block_reason
assert development_repo_block_reason("shizuha-labs", "shizuha") is None
print(json.dumps([canonical_origin_pr_url('o','r',1), origin_ci_logs_url({'repo':'o/r','run_number':'1'})]))
''')
        self.assertEqual(result, ['https://forgejo.example.invalid/o/r/pulls/1', ''])

    def test_external_settings_read_host_csrf_and_mount_configuration(self):
        result = self.run_isolated('''
import json
from taskmanager import settings
print(json.dumps({key:getattr(settings,key) for key in ['ALLOWED_HOSTS','CSRF_TRUSTED_ORIGINS','FORCE_SCRIPT_NAME','PUBLIC_API_PREFIX','SHIZUHA_EXPECTED_ISSUER','PULSE_FLEET_READ_ISSUER','PULSE_OPERATOR_EMAIL','DESIGN_REVIEW_CONVERSATION_ID','SOURCE_AUTHORITY_CONTRACT_URL','SHIZUHA_WIKI_API_URL','SHIZUHA_WIKI_PUBLIC_URL']}))
''', ALLOWED_HOSTS=' tasks.example.test, localhost ',
            CSRF_TRUSTED_ORIGINS='https://tasks.example.test',
            FORCE_SCRIPT_NAME='', PUBLIC_API_PREFIX='',
            SHIZUHA_EXPECTED_ISSUER='https://identity.example.test/id',
            PULSE_OPERATOR_EMAIL='', DESIGN_REVIEW_CONVERSATION_ID='',
            SOURCE_AUTHORITY_CONTRACT_URL='',
            SHIZUHA_WIKI_API_URL='http://wiki:8013/api',
            SHIZUHA_WIKI_PUBLIC_URL='https://platform.example.test/wiki')
        self.assertEqual(result['ALLOWED_HOSTS'], ['tasks.example.test', 'localhost'])
        self.assertEqual(result['CSRF_TRUSTED_ORIGINS'], ['https://tasks.example.test'])
        self.assertIsNone(result['FORCE_SCRIPT_NAME'])
        self.assertEqual(result['PUBLIC_API_PREFIX'], '')
        self.assertEqual(result['SHIZUHA_WIKI_API_URL'], 'http://wiki:8013/api')
        self.assertEqual(result['SHIZUHA_WIKI_PUBLIC_URL'], 'https://platform.example.test/wiki')
        self.assertEqual(result['PULSE_FLEET_READ_ISSUER'], 'https://identity.example.test/id')
        for name in ['PULSE_OPERATOR_EMAIL', 'DESIGN_REVIEW_CONVERSATION_ID', 'SOURCE_AUTHORITY_CONTRACT_URL']:
            self.assertEqual(result[name], '')

    def test_missing_actor_email_never_inherits_unconfigured_operator_authority(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from tasks.services.workflow_service import WorkflowService
        with patch.object(WorkflowService, '_OPERATOR_EMAIL', ''), patch.object(WorkflowService, '_COS_EMAIL', ''):
            self.assertFalse(WorkflowService._actor_may_assign_operator(SimpleNamespace(_activity_actor=None)))
            self.assertFalse(WorkflowService._is_operator_target(None, ''))
            self.assertIsNone(WorkflowService._operator_uid())
            self.assertEqual(WorkflowService._cos_uid_email(), (None, None))
