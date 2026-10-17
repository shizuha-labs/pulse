"""PLAT-10627: batched deploy correlation must filter before slicing.

Django forbids QuerySet.filter() after a slice. The widening pass used to
order_by().[:200] and then filter org, which raised, was swallowed, and left
unkeyed batched events unreachable.
"""
from unittest import mock

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService


class _Query:
    def __init__(self, log):
        self.log = log
        self.sliced = False

    def filter(self, *args, **kwargs):
        if self.sliced:
            raise TypeError("Cannot filter a query once a slice has been taken.")
        self.log.append(("filter", args, kwargs))
        return self

    def order_by(self, *args):
        self.log.append(("order_by", args))
        return self

    def __getitem__(self, item):
        self.log.append(("slice", item))
        self.sliced = True
        return self

    def __iter__(self):
        return iter(())


class _Manager:
    def __init__(self, log):
        self.log = log

    def filter(self, *args, **kwargs):
        self.log.append(("filter", args, kwargs))
        return _Query(self.log)


class BatchedCorrelationQueryOrderTests(SimpleTestCase):
    def test_widening_filters_org_and_success_before_slice(self):
        log = []
        manager = _Manager(log)
        item = mock.Mock()
        item.item_key = "PLAT-10627"
        item.project.org_id = 7
        heads = [{"repo": "shizuha-labs/pulse", "sha": "a" * 40}]

        with mock.patch(
            "tasks.models.DeploymentEvent.objects", manager,
        ):
            found = WorkflowService._find_correlated_deploy_event(item, heads)

        self.assertIsNone(found)
        slices = [i for i, entry in enumerate(log) if entry[0] == "slice"]
        self.assertTrue(slices, log)
        # The widening slice is the second slice (key-named pass slices first).
        widen_at = slices[-1]
        prior_filters = [
            entry for entry in log[:widen_at] if entry[0] == "filter"
        ]
        blob = repr(prior_filters)
        self.assertIn("project__org_id", blob)
        self.assertIn("state__in", blob)
        self.assertIn("smoke_result__iregex", blob)
        self.assertNotIn("filter after slice", blob)
