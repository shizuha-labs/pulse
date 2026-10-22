"""PLAT-12246: a confirmed Origin row must annotate its github.com mirror.

The re-entry gate retires a github.com bounce key only when that exact row
carries ``superseded_by`` pointing at a live-confirmed canonical Origin tuple.
The refresh path wrote that annotation for legacy Origin and git.shizuha.com
rows but never for a github.com row, and the MCP writer only annotates a
mirror when the incoming URL matches an existing row. Re-linking the canonical
Origin URL therefore left the mirror unmarked and the gate wedged forever.
"""

import os
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService


ORIGIN = 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/461'
MIRROR = 'https://github.com/shizuha-labs/deploy/pull/461'


def _response(status):
    return SimpleNamespace(
        ok=True,
        status_code=200,
        json=lambda: {'state': status, 'merged': status == 'merged', 'head': {'sha': 'abc123'}},
    )


class GithubMirrorSupersedeTests(SimpleTestCase):
    def test_confirmed_origin_row_annotates_the_github_mirror(self):
        item = SimpleNamespace(item_key='PLAT-4517', pk=1, metadata={
            'pull_requests': [
                {'url': MIRROR, 'status': 'open'},
                {'url': ORIGIN, 'status': 'open', 'head_sha': 'abc123'},
            ],
        }, save=mock.Mock())

        def fake_request(url, **_kwargs):
            if url.rstrip('/') == ORIGIN:
                return ('origin', 'shizuha-labs', 'deploy', 461, _response('open'))
            return None

        with mock.patch.dict(os.environ, {'FORGEJO_TOKEN': 'test-token'}), mock.patch(
            'tasks.tasks._linked_pr_api_request', side_effect=fake_request,
        ), mock.patch(
            'tasks.tasks._linked_pr_status_from_payload',
            return_value={'status': 'open', 'head_sha': 'abc123'},
        ):
            changed, confirmed = WorkflowService._refresh_review_reentry_pr_metadata(item)

        self.assertTrue(changed)
        rows = {row['url']: row for row in item.metadata['pull_requests']}
        self.assertEqual(rows[MIRROR]['superseded_by'], ORIGIN)
        self.assertTrue(confirmed[ORIGIN])

    def test_unconfirmed_origin_row_leaves_the_mirror_unmarked(self):
        item = SimpleNamespace(item_key='PLAT-4517', pk=1, metadata={
            'pull_requests': [
                {'url': MIRROR, 'status': 'open'},
                {'url': ORIGIN, 'status': 'open'},
            ],
        }, save=mock.Mock())

        with mock.patch.dict(os.environ, {'FORGEJO_TOKEN': 'test-token'}), mock.patch(
            'tasks.tasks._linked_pr_api_request', return_value=None,
        ):
            changed, _confirmed = WorkflowService._refresh_review_reentry_pr_metadata(item)

        self.assertFalse(changed)
        rows = {row['url']: row for row in item.metadata['pull_requests']}
        self.assertNotIn('superseded_by', rows[MIRROR])
