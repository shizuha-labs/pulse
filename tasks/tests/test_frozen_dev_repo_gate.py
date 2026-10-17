from types import SimpleNamespace

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService


class FrozenDevRepoGateTests(SimpleTestCase):
    def test_frozen_origin_shizuha_pr_does_not_count_as_linked(self):
        item = SimpleNamespace(metadata={"pull_requests": [{
            "url": "https://origin.shizuha.com/shizuha-labs/shizuha/pulls/28",
            "repo": "shizuha-labs/shizuha",
            "number": 28,
        }]})
        self.assertFalse(WorkflowService._has_linked_pr(item))
        reason = WorkflowService._frozen_development_pr_reason(item)
        self.assertIsNotNone(reason)
        self.assertIn("shizuha-beta", reason)

    def test_shizuha_beta_pr_counts_as_linked(self):
        item = SimpleNamespace(metadata={"pull_requests": [{
            "url": "https://origin.shizuha.com/shizuha-labs/shizuha-beta/pulls/1",
            "repo": "shizuha-labs/shizuha-beta",
            "number": 1,
        }]})
        self.assertTrue(WorkflowService._has_linked_pr(item))
        self.assertIsNone(WorkflowService._frozen_development_pr_reason(item))
