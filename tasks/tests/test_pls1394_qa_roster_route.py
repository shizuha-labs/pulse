"""PLS-1394: qa-roster evidence is not a code review unless a PR is linked."""
from types import SimpleNamespace

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService


class QaRosterEvidenceRouteTests(SimpleTestCase):
    def test_qa_roster_without_linked_pr_is_evidence_not_code_review(self):
        item = SimpleNamespace(metadata={'routing_mode': 'qa-roster'})
        self.assertTrue(WorkflowService._is_qa_roster_evidence(item))

    def test_qa_roster_with_linked_pr_stays_a_code_review(self):
        item = SimpleNamespace(metadata={
            'routing_mode': 'qa-roster',
            'pull_requests': ['https://origin.shizuha.com/shizuha-labs/pulse/pulls/746'],
        })
        self.assertFalse(WorkflowService._is_qa_roster_evidence(item))
