"""Notifier receipt -> persisted alert -> list/detail serializer -> MCP reader.

The live Computax incident used repository run763 as Forgejo's global ID,
discarded its404, and deployed again as a diagnostic. The notifier had already
stored global39513 and the exact SHA; the read projection lost those fields.
"""
import copy
import os
from pathlib import Path
import sys
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase, TestCase

from tasks.models import Item, Project
from tasks import origin_provenance
from tasks.origin_provenance import (
    normalize_origin_observations,
    origin_observations_with_inference,
)
from tasks.serializers import ItemListSerializer, ItemSerializer
from tasks.views import ItemViewSet

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'mcp-server'))
os.environ.setdefault('SHIZUHA_PULSE_URL', 'http://localhost:8002/pulse/api')
import pulse_server  # noqa: E402


SHA = 'd2b67aaf8d674ac3d8646f4216f77f743bc83c3d'
REPO = 'shizuha-labs/computax'
URL = f'https://origin.shizuha/{REPO}/actions/runs/763'
NOTIFIER = {
    'source': 'origin-ci', 'source_url': URL,
    'description': 'Origin workflow `deploy-hritik.yml` failed on `refs/heads/main`.',
    'payload': {'run_id': '39513', 'commit_sha': SHA, 'status': 'failure'},
    'labels': {'repository': REPO, 'workflow': 'deploy-hritik.yml', 'ref': 'refs/heads/main'},
}


class OriginAlertCallerTests(TestCase):
    def test_notifier_receipt_survives_repeated_list_and_detail_reads(self):
        project = Project.objects.create(key='CIR', org_id=1, name='CI receipt')
        item = Item.objects.create(
            project=project, mode='alert', item_key='CIR-1',
            title='Origin CI failed', status='firing', **copy.deepcopy(NOTIFIER),
        )
        original = (copy.deepcopy(item.payload), copy.deepcopy(item.metadata))
        view = ItemViewSet()
        view.action = 'list'
        for _ in range(2):
            # The real list caller deliberately defers the potentially large
            # payload. Its small scalar projection must suffice without N+1.
            row = view._list_defer_fat_columns(Item.objects.filter(pk=item.pk)).get()
            self.assertIn('payload', row.get_deferred_fields())
            row.description  # Existing legacy-prose read, independent of repair.
            with self.assertNumQueries(0):
                observed = ItemListSerializer().get_origin_observations(row)
            row.project = project
            list_data = ItemListSerializer(row).data
            detail_data = ItemSerializer(Item.objects.get(pk=item.pk)).data
            for data in (list_data, detail_data):
                self.assertEqual(data['origin_observations'], observed)
                self.assertEqual(len(observed), 1)
                self.assertEqual(observed[0]['run_id'], '39513')
                self.assertEqual(observed[0]['run_number'], '763')
                self.assertEqual(observed[0]['commit_sha'], SHA)
                self.assertEqual(observed[0]['workflow'], 'deploy-hritik.yml')
                self.assertEqual(observed[0]['status'], 'failure')
            with mock.patch.object(pulse_server, '_get_my_user_id', return_value=(88, 'ichi@example.test')), \
                 mock.patch.object(pulse_server, 'api_request', return_value={'results': [list_data]}) as api:
                alert_text = pulse_server.get_my_alerts()
                self.assertEqual(api.call_args.args[0], 'GET')
            with mock.patch.object(pulse_server, 'api_request', return_value=detail_data) as api:
                detail_text = pulse_server.get_task(str(item.pk))
                self.assertEqual(api.call_args.args[0], 'GET')
            for text in (alert_text, detail_text):
                self.assertIn('run_number=763', text)
                self.assertIn('run_id=39513', text)
                self.assertIn('commit_sha=' + SHA, text)
                self.assertNotIn('run=39513', text)
                self.assertIn('Logs (GET, Shizuha ID bearer)', text)
                link = text.split('Logs (GET, Shizuha ID bearer): ', 1)[1].split()[0]
                self.assertEqual(urlsplit(link).hostname, 'ci-logs.origin.svc.cluster.local')
                self.assertEqual(parse_qs(urlsplit(link).query), {'repo': [REPO], 'run': ['763']})
            item.refresh_from_db()
            self.assertEqual((item.payload, item.metadata), original)


class OriginAlertIdentityTests(SimpleTestCase):
    def project(self, **changes):
        fields = copy.deepcopy(NOTIFIER)
        fields.update(changes)
        return origin_observations_with_inference({}, **fields)

    def test_missing_payload_preserves_url_number_without_guessing_global_id(self):
        row = self.project(payload={})[0]
        self.assertEqual(row['run_number'], '763')
        self.assertNotIn('run_id', row)
        self.assertNotIn('commit_sha', row)

    def test_conflicting_or_malformed_receipts_are_not_attached_to_url(self):
        cases = (
            {'labels': {**NOTIFIER['labels'], 'repository': 'shizuha-labs/deploy'}},
            {'payload': {**NOTIFIER['payload'], 'run_number': '764'}},
            {'payload': {**NOTIFIER['payload'], 'run_id': True}},
            {'payload': {**NOTIFIER['payload'], 'commit_sha': 'not-a-sha'}},
        )
        for case in cases:
            with self.subTest(case=case):
                row = self.project(**case)[0]
                self.assertEqual(row['run_number'], '763')
                self.assertNotIn('run_id', row)
                self.assertNotIn('commit_sha', row)

    def test_generic_task_payload_is_not_interpreted_as_notifier_receipt(self):
        row = self.project(source='custom-task')[0]
        self.assertEqual(row['run_number'], '763')
        self.assertNotIn('run_id', row)
        self.assertNotIn('commit_sha', row)
        self.assertEqual(origin_observations_with_inference(
            {}, source='custom-task', payload=NOTIFIER['payload'], labels=NOTIFIER['labels'],
        ), [])

    def test_global_id_and_repository_number_collision_remain_separate(self):
        rows = normalize_origin_observations([
            {'repo': REPO, 'run_id': '763', 'commit_sha': 'a' * 40},
            {'run_url': URL, 'commit_sha': SHA},
        ])
        self.assertEqual(len(rows), 2)
        self.assertNotIn('run_number', rows[0])
        self.assertNotIn('run_id', rows[1])

    def test_url_row_joins_complete_same_run_but_not_conflicting_receipt(self):
        complete = self.project()[0]
        rows = normalize_origin_observations([complete, {'run_url': URL}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['run_id'], '39513')
        rows = normalize_origin_observations([{'repo': REPO, 'run_id': '39513'}, complete])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['run_number'], '763')
        conflicting = {**complete, 'run_id': '999', 'commit_sha': 'a' * 40}
        rows = normalize_origin_observations([complete, conflicting])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['commit_sha'], SHA)
        self.assertEqual(rows[1]['commit_sha'], 'a' * 40)

    def test_log_route_never_guesses_number_or_uses_untrusted_url(self):
        for row in (
            {'repo': REPO, 'run_id': '39513'},
            {'repo': REPO, 'run_number': '763', 'run_url': URL.replace('/763', '/764')},
            {'repo': 'evil/r&token=secret', 'run_number': '763'},
            {'repo': REPO, 'run_number': '763', 'run_url': URL.replace('origin.shizuha', 'evil.test')},
        ):
            with self.subTest(row=row):
                self.assertEqual(origin_provenance.origin_ci_logs_url(row), '')

    @mock.patch.dict(os.environ, {'FORGEJO_TOKEN': 'test-only'})
    def test_enrichment_uses_repository_number_and_rejects_ambiguous_or_conflicting_rows(self):
        correct = {'id': 39513, 'index_in_repo': 763, 'commit_sha': SHA, 'workflow_id': 'deploy-hritik.yml'}
        collision = {'id': 763, 'index_in_repo': 42, 'commit_sha': 'a' * 40, 'workflow_id': 'other.yml'}
        for rows, expected in (([collision, correct], '39513'), ([collision], None), ([correct, correct], None)):
            with self.subTest(rows=rows):
                response = mock.Mock()
                response.json.return_value = {'workflow_runs': rows}
                with mock.patch.object(pulse_server.requests, 'get', return_value=response) as get:
                    result = pulse_server._enrich_origin_observation({'run_url': URL})
                    self.assertEqual(get.call_args.kwargs['params'], {'run_number': 763, 'limit': 5})
                    self.assertEqual(result.get('run_id'), expected)
        response.json.return_value = {'workflow_runs': [correct]}
        with mock.patch.object(pulse_server.requests, 'get', return_value=response):
            result = pulse_server._enrich_origin_observation({'run_url': URL, 'run_id': '999', 'commit_sha': 'a' * 40})
            self.assertEqual(result['run_id'], '999')
            self.assertEqual(result['commit_sha'], 'a' * 40)

    @mock.patch.dict(os.environ, {'FORGEJO_TOKEN': 'test-only'})
    def test_http_failure_preserves_original_receipt_without_invented_metadata(self):
        response = mock.Mock()
        response.raise_for_status.side_effect = pulse_server.requests.HTTPError('404')
        with mock.patch.object(pulse_server.requests, 'get', return_value=response):
            result = pulse_server._enrich_origin_observation({'run_url': URL})
        self.assertEqual(result['run_number'], '763')
        self.assertNotIn('run_id', result)
        response.json.assert_not_called()
