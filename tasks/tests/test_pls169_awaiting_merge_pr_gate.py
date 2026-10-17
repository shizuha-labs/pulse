"""PLS-169 — a task must have a linked PR to ENTER awaiting_merge.

SCLI-153 reached `awaiting_merge` ("ready to merge") with ZERO open PRs — its
work was only on fork branches and its "linked PRs" were stale unrelated merged
PRs. The existing review-entry gate (PLS-14) only fires on entering a review
status, not on review→awaiting_merge, so work silently stranded in a false
ready-to-merge state. This adds a blanket merge-path gate.
"""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


def _item(*, pulls=None, labels=None, tags=None, status='in_review'):
    return SimpleNamespace(
        metadata={'pull_requests': pulls} if pulls is not None else {},
        labels=labels or [],
        tags=tags or [],
        status=status,
        project=None,
        is_recurring=False,
    )


def _transition(from_status, to_status, validators=None):
    return SimpleNamespace(
        validators=validators or [],
        from_status=from_status,
        to_status=to_status,
    )


class AwaitingMergePrGateTests(SimpleTestCase):
    def test_enter_awaiting_merge_without_pr_blocked(self):
        item = _item(pulls=[])  # no linked PR
        t = _transition('in_review', 'awaiting_merge')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(t, item)

    def test_enter_awaiting_merge_with_linked_pr_allowed(self):
        item = _item(pulls=[{'url': 'https://github.com/shizuha-labs/x/pull/7', 'number': 7}])
        t = _transition('in_review', 'awaiting_merge')
        WorkflowService._run_validators(t, item)  # must not raise

    def test_no_pr_label_does_not_exempt_merge_path(self):
        # no-pr is for the docs/Approve-&-Close path, NOT the code-merge path
        item = _item(pulls=[], labels=['no-pr'])
        t = _transition('in_review', 'awaiting_merge')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(t, item)

    def test_reentry_within_awaiting_merge_not_gated(self):
        # already at awaiting_merge → not a fresh entry; do not re-gate
        item = _item(pulls=[], status='awaiting_merge')
        t = _transition('awaiting_merge', 'awaiting_merge')
        WorkflowService._run_validators(t, item)  # must not raise

    def test_hyphenated_status_slug_tolerated(self):
        item = _item(pulls=[])
        t = _transition('in-review', 'awaiting-merge')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(t, item)


class HasLinkedPrHelperTests(SimpleTestCase):
    def test_pr_entry_true(self):
        self.assertTrue(WorkflowService._has_linked_pr(_item(pulls=[{'url': 'u'}])))
        self.assertTrue(WorkflowService._has_linked_pr(_item(pulls=[{'number': 3}])))

    def test_no_pr_flag_is_not_a_linked_pr(self):
        self.assertFalse(WorkflowService._has_linked_pr(_item(pulls=[], labels=['no-pr'])))

    def test_empty_false(self):
        self.assertFalse(WorkflowService._has_linked_pr(_item(pulls=[])))
        self.assertFalse(WorkflowService._has_linked_pr(_item()))
