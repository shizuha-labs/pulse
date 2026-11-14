"""PLAT-7527: stale-artifact flag exempts GitHub-era PR links from the open-PR gate.

GitHub-era linked-PR entries whose numbers Origin re-used for unrelated PRs can
never reach a terminal status of their own and permanently wedge terminal close
under the PLAT-4719 open-PR guard — even when the task's real fix is merged and
fully verified (live carrier: SCS-82). The remediation is an explicit,
admin-set ``stale_artifact`` flag on the linked entry:

* the open-PR gate (WorkflowService._has_open_linked_prs and
  tasks._item_has_open_linked_pr) exempts flagged rows — fail-closed preserved
  because only admins can set the flag and every unflagged open row blocks;
* POST /api/items/{id}/pull-requests/stale-artifact/ sets/unsets the flag,
  admin-only, matched by exact URL, with an ItemActivity audit row.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService


STALE_GITHUB_ERA = {
    'url': 'https://github.com/shizuha-labs/scs/pull/43',
    'repo': 'shizuha-labs/scs',
    'number': 43,
    'status': 'open',
}
STALE_GITHUB_ERA_FLAGGED = dict(STALE_GITHUB_ERA, stale_artifact=True)
GENUINE_OPEN_ORIGIN = {
    'url': 'https://origin.shizuha.com/shizuha-labs/scs/pulls/99',
    'repo': 'shizuha-labs/scs',
    'number': 99,
    'status': 'open',
}


def _item(*, prs, key='SCS-82'):
    return SimpleNamespace(
        item_key=key,
        pk=1,
        status='verification',
        workflow_status='verification',
        workflow_name='security-finding',
        labels=[],
        tags=[],
        metadata={'pull_requests': list(prs)},
        comments=mock.Mock(),
        activities=mock.Mock(),
    )


class StaleArtifactGuardExemptionTests(SimpleTestCase):
    def test_unflagged_stale_github_era_open_pr_still_blocks(self):
        """Fail-closed: without the flag the guard behaves exactly as before."""
        item = _item(prs=[STALE_GITHUB_ERA])
        self.assertTrue(WorkflowService._has_open_linked_prs(item))

    def test_flagged_stale_artifact_is_exempt(self):
        item = _item(prs=[STALE_GITHUB_ERA_FLAGGED])
        self.assertFalse(WorkflowService._has_open_linked_prs(item))

    def test_flagged_row_does_not_mask_a_real_open_origin_pr(self):
        """The exemption is per-row: a genuine open Origin PR still blocks."""
        item = _item(prs=[STALE_GITHUB_ERA_FLAGGED, GENUINE_OPEN_ORIGIN])
        self.assertTrue(WorkflowService._has_open_linked_prs(item))

    def test_flagged_row_does_not_create_merged_origin_authority(self):
        """A flagged row must not count as merged delivery authority either."""
        item = _item(prs=[STALE_GITHUB_ERA_FLAGGED])
        merged_origin = any(
            WorkflowService._pr_is_merged_entry(pr)
            and not WorkflowService._is_retired_mirror_pr(pr)
            for pr in WorkflowService._linked_pr_entries(item)
        )
        self.assertFalse(merged_origin)

    def test_tasks_module_guard_skips_flagged_rows(self):
        """tasks._item_has_open_linked_pr honors the same flag."""
        from tasks.tasks import _item_has_open_linked_pr

        self.assertFalse(_item_has_open_linked_pr(_item(prs=[STALE_GITHUB_ERA_FLAGGED])))
        self.assertTrue(_item_has_open_linked_pr(_item(prs=[STALE_GITHUB_ERA])))
        self.assertTrue(
            _item_has_open_linked_pr(_item(prs=[STALE_GITHUB_ERA_FLAGGED, GENUINE_OPEN_ORIGIN]))
        )


class StaleArtifactEndpointTests(SimpleTestCase):
    """Exercise the admin-gated flag endpoint logic without a DB."""

    def _view(self):
        from tasks.views import ItemPullRequestStaleArtifactView

        return ItemPullRequestStaleArtifactView()

    def _request(self, user, body):
        from rest_framework.test import APIRequestFactory

        request = APIRequestFactory().post('/api/items/SCS-82/pull-requests/stale-artifact/', body, format='json')
        request.user = user
        return request

    def _admin(self, *, superuser=True):
        return SimpleNamespace(is_superuser=superuser, is_staff=False, id=7, email='goro@agents.shizuha.io')

    def _patched(self, item):
        return (
            mock.patch('tasks.views._resolve_item', return_value=item),
            mock.patch('tasks.views.ItemActivity.objects.create'),
        )

    def test_non_admin_gets_403(self):
        item = _item(prs=[STALE_GITHUB_ERA])
        item.save = mock.Mock()
        p1, p2 = self._patched(item)
        with p1, p2:
            resp = self._view().post(
                self._request(self._admin(superuser=False), {'url': STALE_GITHUB_ERA['url'], 'stale_artifact': True, 'reason': 'numbering collision'}),
                'SCS-82',
            )
        self.assertEqual(resp.status_code, 403)
        item.save.assert_not_called()

    def test_admin_can_flag_and_unflag(self):
        item = _item(prs=[dict(STALE_GITHUB_ERA)])
        item.save = mock.Mock()
        p1, p2 = self._patched(item)
        with p1, p2:
            resp = self._view().post(
                self._request(self._admin(), {'url': STALE_GITHUB_ERA['url'], 'stale_artifact': True, 'reason': 'GitHub-era link; Origin re-used #43 for unrelated PLAT-5287 work'}),
                'SCS-82',
            )
        self.assertEqual(resp.status_code, 200, getattr(resp, 'data', None))
        self.assertTrue(item.metadata['pull_requests'][0]['stale_artifact'])
        item.save.assert_called_once()

        # Unflag clears the flag and its provenance keys.
        with p1, p2:
            resp = self._view().post(
                self._request(self._admin(), {'url': STALE_GITHUB_ERA['url'], 'stale_artifact': False}),
                'SCS-82',
            )
        self.assertEqual(resp.status_code, 200)
        entry = item.metadata['pull_requests'][0]
        self.assertFalse(entry.get('stale_artifact'))
        self.assertNotIn('stale_artifact_reason', entry)

    def test_unknown_url_is_404_and_short_reason_rejected(self):
        item = _item(prs=[dict(STALE_GITHUB_ERA)])
        item.save = mock.Mock()
        p1, p2 = self._patched(item)
        with p1, p2:
            resp = self._view().post(
                self._request(self._admin(), {'url': 'https://github.com/shizuha-labs/scs/pull/999', 'stale_artifact': True, 'reason': 'wrong row on purpose'}),
                'SCS-82',
            )
        self.assertEqual(resp.status_code, 404)
        item.save.assert_not_called()

        with p1, p2:
            resp = self._view().post(
                self._request(self._admin(), {'url': STALE_GITHUB_ERA['url'], 'stale_artifact': True, 'reason': 'short'}),
                'SCS-82',
            )
        self.assertEqual(resp.status_code, 400)
        item.save.assert_not_called()
