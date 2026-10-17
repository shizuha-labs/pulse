"""PLS-1249: CI-only / no-deploy-surface evidence class + serializer loud-reject.

A dev task whose merged fix touches only tests/docs/CI-config has no deploy
surface — the deploy-verify gate's correlated-DeploymentEvent demand is
structurally unsatisfiable for it (live instance: SCLI-723, beta#364 touching
only a TUI e2e test). The gate now derives the class from the MERGED CONTENT
and accepts the merged head's green CI run instead. Secondary defect: the
item serializer silently dropped read-only ``acceptance_mode`` on PATCH
(HTTP 200, field unchanged) — it now rejects loudly with 400.
"""
from unittest import mock

from django.test import TestCase

from tasks.models import Item, Project
from tasks.services import ci_only_evidence
from tasks.services.workflow_service import WorkflowService


class CiOnlyPathClassificationTests(TestCase):
    def test_test_doc_ci_paths_are_ci_only(self):
        for p in (
            'tests/tui/e2e-tmux-resumed-transcript.test.ts',
            'test_auth.py', 'auth_test.go', 'conftest.py',
            'docs/runbook.md', 'README.md', 'CHANGELOG.rst',
            '.forgejo/workflows/deploy.yml', '.github/workflows/ci.yaml',
            'pyproject.toml', 'setup.cfg', 'pytest.ini', 'Makefile',
            'scripts/ci/lint.sh',
        ):
            self.assertTrue(ci_only_evidence._path_is_ci_only(p), p)

    def test_source_and_deploy_paths_are_not_ci_only(self):
        for p in (
            'inference/reconciler.py', 'src/connect-client/index.ts',
            'k3s/monitoring/deployment.yaml', 'docker-compose.yml',
            'Dockerfile', 'app/models/user.py', 'manage.py',
        ):
            self.assertFalse(ci_only_evidence._path_is_ci_only(p), p)

    def test_empty_path_is_not_ci_only(self):
        self.assertFalse(ci_only_evidence._path_is_ci_only(''))
        self.assertFalse(ci_only_evidence._path_is_ci_only(None))


class MergedContentCiOnlyGateTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='P', org_id=1)

    def _item(self, *, acceptance_mode='live'):
        return Item.objects.create(
            project=self.project,
            title='[BUG] master-red TUI e2e fix',
            description='test-only fix',
            labels=[], tags='',
            status='in_review',
            workflow_name='autonomous-bug',
            item_type='task',
            acceptance_mode=acceptance_mode,
            created_by_id=1,
            created_by_email='jun@shizuha.com',
            metadata={'pull_requests': [{
                'url': 'https://origin.shizuha.com/shizuha-labs/shizuha-beta/pulls/364',
                'repo': 'shizuha-labs/shizuha-beta',
                'number': 364,
                'status': 'merged',
                'merged_at': '2026-09-14T10:00:00Z',
                'head_sha': 'deadbeefcafe1234567890abcdef1234567890ab',
            }]},
        )

    def test_ci_only_content_with_green_head_closes_without_deploy_event(self):
        """Acceptance: a test-only merged fix closes on CI-run evidence — no
        correlated DeploymentEvent demanded, no data mutation."""
        item = self._item()
        with mock.patch.object(ci_only_evidence, 'pr_files_are_ci_only',
                               return_value=True), \
             mock.patch.object(ci_only_evidence, 'head_ci_green',
                               return_value=True):
            missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, [])

    def test_ci_only_content_with_non_green_head_fails_closed(self):
        item = self._item()
        with mock.patch.object(ci_only_evidence, 'pr_files_are_ci_only',
                               return_value=True), \
             mock.patch.object(ci_only_evidence, 'head_ci_green',
                               return_value=False):
            missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertEqual(missing, ['missing_ci_run_evidence'])

    def test_non_ci_only_content_keeps_governed_path(self):
        """A source file in the merged PR → the class never applies; the
        governed deploy-verify demand is unchanged."""
        item = self._item()
        with mock.patch.object(ci_only_evidence, 'pr_files_are_ci_only',
                               return_value=False):
            missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertIn('missing_correlated_deploy_event', missing)

    def test_fetch_failure_fails_closed(self):
        """Unknown (fetch error / empty files) → governed path, never a
        silent exemption."""
        item = self._item()
        with mock.patch.object(ci_only_evidence, 'pr_files_are_ci_only',
                               return_value=None):
            missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertIn('missing_correlated_deploy_event', missing)

    def test_no_merged_heads_never_enters_the_class(self):
        item = self._item()
        item.metadata = {'pull_requests': []}
        item.save()
        with mock.patch.object(ci_only_evidence, 'pr_files_are_ci_only',
                               return_value=True) as files_mock, \
             mock.patch.object(ci_only_evidence, 'head_ci_green',
                               return_value=True):
            missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        files_mock.assert_not_called()
        self.assertIn('missing_merged_head_sha', missing)


class AcceptanceModePatchLoudRejectTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='P2', org_id=1)
        self.item = Item.objects.create(
            project=self.project,
            title='[BUG] x',
            description='d',
            labels=[], tags='',
            status='in_review',
            workflow_name='autonomous-bug',
            item_type='task',
            acceptance_mode='live',
            created_by_id=1,
            created_by_email='jun@shizuha.com',
        )

    def test_update_with_acceptance_mode_rejects_loudly(self):
        from tasks.serializers import ItemSerializer
        serializer = ItemSerializer(
            self.item,
            data={'title': '[BUG] x2', 'acceptance_mode': 'code'},
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('acceptance_mode', serializer.errors)

    def test_update_without_acceptance_mode_is_unaffected(self):
        from tasks.serializers import ItemSerializer
        serializer = ItemSerializer(
            self.item, data={'title': '[BUG] x2'}, partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_create_still_accepts_acceptance_mode(self):
        from tasks.serializers import ItemSerializer
        serializer = ItemSerializer(data={
            'project': self.project.pk,
            'title': '[BUG] fresh',
            'description': 'd',
            'status': 'todo',
            'workflow_name': 'autonomous-bug',
            'item_type': 'task',
            'acceptance_mode': 'code',
            'created_by_id': 1,
            'created_by_email': 'jun@shizuha.com',
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
