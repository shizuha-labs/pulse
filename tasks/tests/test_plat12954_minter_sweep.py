"""PLAT-12954: minter sweep unit tests (offline; HTTP mocked).

Contracts under test (all fail-closed):
  - FORGEJO_TOKEN absent -> CommandError (the minter never runs without the
    pulse deployment context's own authority).
  - A converged+probed run on the production head mints a smoke_passed
    DeploymentEvent via the SAME upsert service the staff-only ingest view
    uses, with the minted_by provenance in metadata.
  - Runs already in the ledger (same idempotency key the ingest lane
    computes) are skipped.
  - Runs whose log is unverifiable (no SHIZUHA_ID_TOKEN / fetch failure /
    incomplete proof) are SKIPPED, never fabricated.
  - Runs whose head SHA is not on the production head are skipped.
  - _proof_converged gates: generation observed >= desired, replicas
    ready == desired, UID + digest present.
"""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from tasks.management.commands.mint_deployment_events import (
    Command as MinterCommand,
)
from tasks.models import DeploymentEvent

HEAD_SHA = 'a' * 40
RUN_ID = '10499'

RUNS_RESPONSE = {
    'workflow_runs': [
        {'id': RUN_ID, 'status': 'success', 'head_sha': HEAD_SHA,
         'path': '.forgejo/workflows/deploy.yml',
         'run_started_at': '2026-11-10T10:00:00Z'},
    ],
}

REPO_INFO = {'default_branch': 'main'}

COMMITS_RESPONSE = [{'sha': HEAD_SHA}, {'sha': 'b' * 40}]

PROOF_LOG = (
    'deployment uid=0f1e2d3c-1111-2222-3333-444455556666\n'
    'generation=7 observedGeneration=7\n'
    'spec.replicas=3 readyReplicas=3\n'
    'imageID=docker.io/shizuha/hive@sha256:'
    + 'c' * 64 + '\n'
)


class MinterSweepTests(TestCase):
    def _call(self, **opts):
        out = StringIO()
        base = {
            'window_hours': 48, 'org': 'shizuha-labs',
            'forgejo_url': 'http://forgejo.test', 'ci_logs_url':
            'http://cilogs.test', 'workflow': 'deploy.yml',
            'repos': 'hive', 'dry_run': False,
        }
        base.update(opts)
        call_command('mint_deployment_events', stdout=out, **base)
        return out.getvalue()

    def test_fail_closed_without_forgejo_token(self):
        import os
        old = os.environ.pop('FORGEJO_TOKEN', None)
        try:
            with self.assertRaises(CommandError):
                self._call()
        finally:
            if old is not None:
                os.environ['FORGEJO_TOKEN'] = old

    @mock.patch.dict('os.environ', {'FORGEJO_TOKEN': 'tok',
                                    'SHIZUHA_ID_TOKEN': 'idtok'})
    @mock.patch('tasks.management.commands.mint_deployment_events._http_get')
    @mock.patch('tasks.management.commands.mint_deployment_events._http_json')
    def test_mints_converged_run(self, json_mock, get_mock):
        def json_side(url, token=None, timeout=30):
            if '/actions/tasks' in url:
                return RUNS_RESPONSE
            if url.endswith('/repos/shizuha-labs/hive'):
                return REPO_INFO
            if '/commits?' in url:
                return COMMITS_RESPONSE
            raise AssertionError(f'unexpected url {url}')

        json_mock.side_effect = json_side
        get_mock.side_effect = lambda url, token=None, timeout=30, headers=None: PROOF_LOG

        self._call()
        event = DeploymentEvent.objects.get(commit_sha=HEAD_SHA)
        self.assertEqual(event.state, 'smoke_passed')
        self.assertEqual(event.smoke_result, 'passed')
        self.assertEqual(event.repo, 'shizuha-labs/hive')
        self.assertEqual(event.service, 'hive')
        self.assertEqual(event.pipeline_run_id, RUN_ID)
        self.assertEqual(
            (event.metadata or {}).get('minted_by'), 'plat12954-minter')
        self.assertTrue(
            (event.metadata or {}).get('rollout_proof', {}).get('image_digest'))

    @mock.patch.dict('os.environ', {'FORGEJO_TOKEN': 'tok',
                                    'SHIZUHA_ID_TOKEN': 'idtok'})
    @mock.patch('tasks.management.commands.mint_deployment_events._http_get')
    @mock.patch('tasks.management.commands.mint_deployment_events._http_json')
    def test_skips_already_ledgered(self, json_mock, get_mock):
        idem = DeploymentEvent.build_idempotency_key(
            repo='shizuha-labs/hive', service='hive', environment='live',
            cluster='shizuha-k3s', commit_sha=HEAD_SHA,
            pipeline_system='origin-forgejo', pipeline_run_id=RUN_ID)
        DeploymentEvent.objects.create(
            idempotency_key=idem, repo='shizuha-labs/hive', service='hive',
            commit_sha=HEAD_SHA, state='smoke_passed')

        def json_side(url, token=None, timeout=30):
            if '/actions/tasks' in url:
                return RUNS_RESPONSE
            if url.endswith('/repos/shizuha-labs/hive'):
                return REPO_INFO
            if '/commits?' in url:
                return COMMITS_RESPONSE
            raise AssertionError(url)

        json_mock.side_effect = json_side
        self._call()
        self.assertEqual(
            DeploymentEvent.objects.filter(commit_sha=HEAD_SHA).count(), 1)

    @mock.patch.dict('os.environ', {'FORGEJO_TOKEN': 'tok'})
    @mock.patch('tasks.management.commands.mint_deployment_events._http_json')
    def test_skips_unverifiable_log_without_id_token(self, json_mock):
        """No SHIZUHA_ID_TOKEN -> log fetch unverifiable -> fail-closed skip."""
        def json_side(url, token=None, timeout=30):
            if '/actions/tasks' in url:
                return RUNS_RESPONSE
            if url.endswith('/repos/shizuha-labs/hive'):
                return REPO_INFO
            if '/commits?' in url:
                return COMMITS_RESPONSE
            raise AssertionError(url)

        json_mock.side_effect = json_side
        out = self._call()
        self.assertIn('"skipped_unverifiable": 1', out)
        self.assertIn('"minted": 0', out)
        self.assertFalse(
            DeploymentEvent.objects.filter(commit_sha=HEAD_SHA).exists())

    @mock.patch.dict('os.environ', {'FORGEJO_TOKEN': 'tok',
                                    'SHIZUHA_ID_TOKEN': 'idtok'})
    @mock.patch('tasks.management.commands.mint_deployment_events._http_json')
    def test_skips_run_not_on_production_head(self, json_mock):
        def json_side(url, token=None, timeout=30):
            if '/actions/tasks' in url:
                return RUNS_RESPONSE
            if url.endswith('/repos/shizuha-labs/hive'):
                return REPO_INFO
            if '/commits?' in url:
                return [{'sha': 'e' * 40}]  # HEAD_SHA absent
            raise AssertionError(url)

        json_mock.side_effect = json_side
        out = self._call()
        self.assertIn('"skipped_not_on_production_head": 1', out)
        self.assertFalse(
            DeploymentEvent.objects.filter(commit_sha=HEAD_SHA).exists())


class ProofConvergedTests(TestCase):
    def _conv(self, proof):
        return MinterCommand()._proof_converged(proof)

    def test_complete_proof_converges(self):
        self.assertTrue(self._conv({
            'deployment_uid': '0f1e2d3c-1111-2222-3333-444455556666',
            'generation': '7', 'desired_generation': '7',
            'ready_replicas': '3', 'desired_replicas': '3',
            'image_digest': 'c' * 64,
        }))

    def test_stale_generation_does_not_converge(self):
        self.assertFalse(self._conv({
            'deployment_uid': 'x', 'generation': '6',
            'desired_generation': '7', 'ready_replicas': '3',
            'desired_replicas': '3', 'image_digest': 'c' * 64,
        }))

    def test_replica_shortfall_does_not_converge(self):
        self.assertFalse(self._conv({
            'deployment_uid': 'x', 'generation': '7',
            'desired_generation': '7', 'ready_replicas': '2',
            'desired_replicas': '3', 'image_digest': 'c' * 64,
        }))

    def test_missing_uid_or_digest_does_not_converge(self):
        self.assertFalse(self._conv({
            'generation': '7', 'desired_generation': '7',
            'ready_replicas': '3', 'desired_replicas': '3',
        }))
