import os
import unittest
from unittest import mock

os.environ.setdefault('SHIZUHA_PULSE_URL', 'http://localhost:8002/pulse/api')

import pulse_server  # noqa: E402


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {'workflow_runs': [{
            'id': 6478,
            'index_in_repo': 649,
            'commit_sha': 'bf0d3c461a0504f8ec7bf14e572a5830d2785314',
            'prettyref': 'refs/pull/39/head',
            'workflow_id': 'deploy-origin-ci-helm.yml',
            'status': 'failure',
            'stopped': '2026-07-16T13:15:55Z',
        }]}


class OriginProvenanceToolTests(unittest.TestCase):
    def test_render_is_repo_and_commit_qualified(self):
        rendered = pulse_server._fmt_origin_observations({
            'origin_observations': [{
                'repo': 'shizuha-labs/deploy',
                'commit_sha': 'bf0d3c461a0504f8ec7bf14e572a5830d2785314',
                'run_number': '649',
                'job_id': '0',
                'status': 'failure',
                'run_url': 'https://origin.shizuha.com/shizuha-labs/deploy/actions/runs/649/jobs/0',
            }],
        }, include_url=True)
        self.assertIn('shizuha-labs/deploy@bf0d3c461a05', rendered)
        self.assertIn('run_number=649', rendered)
        self.assertIn('status=failure', rendered)
        self.assertIn('/actions/runs/649/jobs/0', rendered)

    @mock.patch.dict(os.environ, {'FORGEJO_TOKEN': 'test-token'})
    @mock.patch.object(pulse_server.requests, 'get', return_value=_Response())
    def test_run_url_enriches_to_full_forgejo_identity(self, get):
        enriched = pulse_server._enrich_origin_observation({
            'run_url': '/shizuha-labs/deploy/actions/runs/649/jobs/0',
        })
        self.assertEqual(enriched['repo'], 'shizuha-labs/deploy')
        self.assertEqual(enriched['run_id'], '6478')
        self.assertEqual(enriched['run_number'], '649')
        self.assertEqual(
            enriched['commit_sha'],
            'bf0d3c461a0504f8ec7bf14e572a5830d2785314',
        )
        self.assertEqual(enriched['workflow'], 'deploy-origin-ci-helm.yml')
        self.assertEqual(enriched['status'], 'failure')
        self.assertEqual(
            get.call_args.kwargs['params'],
            {'run_number': 649, 'limit': 5},
        )

    @mock.patch.object(pulse_server, 'api_request')
    @mock.patch.object(pulse_server, '_enrich_origin_observation')
    def test_link_tool_persists_structured_evidence(self, enrich, api_request):
        enrich.return_value = {
            'provider': 'origin-forgejo',
            'repo': 'shizuha-labs/deploy',
            'commit_sha': 'bf0d3c461a0504f8ec7bf14e572a5830d2785314',
            'run_number': '649',
            'run_url': 'https://origin.shizuha.com/shizuha-labs/deploy/actions/runs/649',
        }
        api_request.side_effect = [
            {'id': 20376, 'item_key': 'PLAT-4676', 'metadata': {}},
            {'id': 20376, 'item_key': 'PLAT-4676'},
        ]
        result = pulse_server.link_origin_run(
            'PLAT-4676',
            'https://origin.shizuha.com/shizuha-labs/deploy/actions/runs/649',
        )
        self.assertIn('PLAT-4676', result)
        patch = api_request.call_args_list[1].kwargs['data']
        row = patch['append_origin_observations'][0]
        self.assertEqual(row['commit_sha'], enrich.return_value['commit_sha'])


if __name__ == '__main__':
    unittest.main()
