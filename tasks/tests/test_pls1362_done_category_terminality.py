"""PLS-1362: terminality must cover every done-category status slug.

The dead-end sweep keys terminality on slug membership for the hot path.
Any Status row whose category is 'done' but whose slug is absent from the
canonical sets is falsely flagged non-terminal — applied/rejected/
risk_accepted/deferred class — which re-routes resolved work against its
seat (the PLS-1362 churn signature).
"""
from django.test import TestCase

from tasks.models import Status
from tasks.services.actionable_items import TERMINAL_STATUS_SLUGS
from tasks.services.workflow_service import WorkflowService


class PLS1362DoneCategoryTerminalityTest(TestCase):
    def _done_category_slugs(self):
        return set(
            Status.objects.filter(category='done').values_list('slug', flat=True)
        )

    def test_canonical_sets_cover_all_done_category_slugs(self):
        missing_actionable = self._done_category_slugs() - set(TERMINAL_STATUS_SLUGS)
        missing_service = self._done_category_slugs() - set(
            WorkflowService.TERMINAL_STATUS_SLUGS
        )
        self.assertEqual(
            missing_actionable, set(),
            f'actionable_items.TERMINAL_STATUS_SLUGS is missing done-category slugs: {sorted(missing_actionable)}',
        )
        self.assertEqual(
            missing_service, set(),
            f'WorkflowService.TERMINAL_STATUS_SLUGS is missing done-category slugs: {sorted(missing_service)}',
        )

    def test_applied_and_risk_accepted_are_terminal(self):
        self.assertIn('applied', TERMINAL_STATUS_SLUGS)
        self.assertIn('risk_accepted', TERMINAL_STATUS_SLUGS)
        self.assertTrue(WorkflowService._status_is_terminal('risk_accepted'))
        self.assertTrue(WorkflowService._status_is_terminal('applied'))
