"""PLAT-979: CoS-pinned tasks are excluded from the rebalance-sweep movable set."""
from types import SimpleNamespace
from django.test import SimpleTestCase

from tasks.tasks import _is_cos_pinned
from tasks.services.workflow_service import WorkflowService


class CosPinnedRebalanceExclusionTest(SimpleTestCase):
    def _item(self, labels=None, group='', email=''):
        return SimpleNamespace(labels=labels or [], assignment_group=group, assignee_email=email)

    def test_admin_ops_cos_assignee_is_pinned(self):
        self.assertTrue(self._is_pinned(group='admin-ops', email=WorkflowService._COS_EMAIL))

    def test_cos_pinned_label_is_pinned_regardless_of_team(self):
        self.assertTrue(self._is_pinned(labels=['cos-pinned'], group='engineering', email='x@shizuha.com'))

    def test_normal_engineering_task_not_pinned(self):
        self.assertFalse(self._is_pinned(group='engineering', email='sara@shizuha.com'))

    def test_admin_ops_non_cos_assignee_not_pinned(self):
        self.assertFalse(self._is_pinned(group='admin-ops', email='someoneelse@shizuha.com'))

    def test_blank_task_not_pinned(self):
        self.assertFalse(self._is_pinned())

    def _is_pinned(self, labels=None, group='', email=''):
        return _is_cos_pinned(self._item(labels, group, email))
