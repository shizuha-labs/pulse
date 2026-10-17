"""PLAT-5675: terminal close blocked while linked PRs remain open."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


OPEN_PR = {
    'url': 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/341',
    'repo': 'shizuha-labs/deploy',
    'number': 341,
    'status': 'open',
    'head_sha': 'a' * 40,
}
MERGED_PR = {
    'url': 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/341',
    'repo': 'shizuha-labs/deploy',
    'number': 341,
    'status': 'merged',
    'head_sha': 'a' * 40,
}


def _item(*, labels=None, prs=None, key='PLAT-5667', status='awaiting_merge'):
    return SimpleNamespace(
        item_key=key,
        pk=1,
        status=status,
        workflow_status=status,
        workflow_name='autonomous-bug',
        labels=labels or [],
        tags=[],
        metadata={'pull_requests': list(prs or [])},
        comments=mock.Mock(),
        activities=mock.Mock(),
        save=mock.Mock(),
        assignee_email='author@agents.shizuha.io',
        assignee_id=1,
        assignment_group=None,
        resolution_id=None,
        project=None,
        project_id=None,
        mode='task',
    )


class LinkedPrTerminalGateUnitTests(SimpleTestCase):
    def test_open_pr_blocks_done(self):
        item = _item(prs=[OPEN_PR])
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata', return_value=(False, {}),
        ):
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService._validate_linked_pr_terminal_gate(item, 'done')
        self.assertIn('still open', str(ctx.exception))
        self.assertIn('341', str(ctx.exception))
        self.assertIn('PLAT-5675', str(ctx.exception))

    def test_open_pr_blocks_merged_closed_applied(self):
        item = _item(prs=[OPEN_PR])
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata', return_value=(False, {}),
        ):
            for target in ('merged', 'closed', 'applied', 'completed', 'resolved'):
                with self.subTest(target=target):
                    with self.assertRaises(WorkflowValidationError) as ctx:
                        WorkflowService._validate_linked_pr_terminal_gate(item, target)
                    self.assertIn('PLAT-5675', str(ctx.exception))

    def test_abandon_terminals_not_gated(self):
        """cancelled/rejected/deferred assert abandon, not delivery."""
        item = _item(prs=[OPEN_PR])
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata', return_value=(False, {}),
        ):
            for target in ('cancelled', 'rejected', 'deferred', 'wont_fix', 'failed'):
                with self.subTest(target=target):
                    WorkflowService._validate_linked_pr_terminal_gate(item, target)

    def test_merged_pr_allows_done(self):
        item = _item(prs=[MERGED_PR])
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata', return_value=(False, {}),
        ):
            WorkflowService._validate_linked_pr_terminal_gate(item, 'done')

    def test_no_pr_label_only_when_no_linked_prs(self):
        # no linked PRs + no-pr → exempt
        item = _item(labels=['no-pr'], prs=[])
        with mock.patch.object(WorkflowService, '_item_has_label', side_effect=lambda i, l: l == 'no-pr'):
            WorkflowService._validate_linked_pr_terminal_gate(item, 'done')

    def test_no_pr_label_does_not_waive_open_linked_prs(self):
        item = _item(labels=['no-pr'], prs=[OPEN_PR])
        with mock.patch.object(
            WorkflowService, '_item_has_label', side_effect=lambda i, l: l == 'no-pr',
        ), mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata', return_value=(False, {}),
        ):
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService._validate_linked_pr_terminal_gate(item, 'done')
        self.assertIn('still open', str(ctx.exception))

    def test_superseded_open_row_ignored(self):
        item = _item(prs=[{
            'url': 'https://origin.shizuha.com/shizuha-labs/deploy/pull/341',
            'status': 'open',
            'superseded_by': 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/341',
        }, MERGED_PR])
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata', return_value=(False, {}),
        ):
            WorkflowService._validate_linked_pr_terminal_gate(item, 'done')


class LinkedPrTerminalGateCallSiteTests(SimpleTestCase):
    """reika P2-2: prove validate_transition invokes the gate (not only the helper)."""

    def test_validate_transition_refuses_done_with_open_pr(self):
        item = _item(prs=[OPEN_PR], status='awaiting_merge')
        # Short-circuit heavy paths; keep the PLAT-5675 call live.
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata', return_value=(False, {}),
        ), mock.patch.object(
            WorkflowService, '_higher_impact_owned_blocker', return_value=None,
        ), mock.patch.object(
            WorkflowService, '_validate_deploy_verify_gate', return_value=None,
        ), mock.patch.object(
            WorkflowService, '_validate_stride_gate', return_value=None,
        ), mock.patch(
            'tasks.services.urgent_risk_containment.assert_transition_allowed',
            return_value=None,
        ), mock.patch.object(
            WorkflowService, '_has_active_blocked_descendants', return_value=False,
        ), mock.patch(
            'tasks.liveness.record_heartbeat', return_value=None,
        ):
            # Avoid DB workflow lookup by clearing workflow_name after gate would fire.
            # Gate runs before workflow_name branch — keep name for realism but mock resolve.
            with mock.patch(
                'tasks.models.Workflow.objects.filter',
            ) as wf_filter:
                wf_filter.return_value.first.return_value = None
                # Also mock project scheme path fall-through
                item.project = None
                with self.assertRaises(WorkflowValidationError) as ctx:
                    WorkflowService.validate_transition(item, 'done')
        self.assertIn('PLAT-5675', str(ctx.exception))
        self.assertIn('still open', str(ctx.exception))

    def test_validate_transition_allows_done_when_merged(self):
        item = _item(prs=[MERGED_PR], status='awaiting_merge')
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata', return_value=(False, {}),
        ), mock.patch.object(
            WorkflowService, '_higher_impact_owned_blocker', return_value=None,
        ), mock.patch.object(
            WorkflowService, '_validate_deploy_verify_gate', return_value=None,
        ), mock.patch.object(
            WorkflowService, '_validate_stride_gate', return_value=None,
        ), mock.patch(
            'tasks.services.urgent_risk_containment.assert_transition_allowed',
            return_value=None,
        ), mock.patch.object(
            WorkflowService, '_has_active_blocked_descendants', return_value=False,
        ), mock.patch(
            'tasks.liveness.record_heartbeat', return_value=None,
        ), mock.patch(
            'tasks.models.Workflow.objects.filter',
        ) as wf_filter:
            wf_filter.return_value.first.return_value = None
            item.project = None
            # May still fail later on missing workflow — only assert gate did not fire.
            try:
                WorkflowService.validate_transition(item, 'done')
            except WorkflowValidationError as exc:
                self.assertNotIn('PLAT-5675', str(exc))



MIRROR_PR = {
    'url': 'https://github.com/shizuha-labs/deploy/pull/341',
    'repo': 'shizuha-labs/deploy',
    'number': 341,
    'status': 'open',
}


class LinkedPrTerminalGateCanonicalControlTests(SimpleTestCase):
    """rui P2 (PLAT-5675): gate reuses _has_open_linked_prs semantics.

    The open-PR determination must be identical to the deploy-verify gate —
    not a stricter reimplementation — so the PLS-675 wedge (PLAT-5854 mirror
    duplicate) and merged_at-implies-merged are honored here too.
    """

    def test_retired_mirror_pr_with_merged_origin_allows_done(self):
        """PLAT-5854: stale open github.com mirror PR alongside a merged Origin
        PR is a superseded duplicate, not an open obligation."""
        item = _item(prs=[MIRROR_PR, MERGED_PR])
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata', return_value=(False, {}),
        ):
            WorkflowService._validate_linked_pr_terminal_gate(item, 'done')

    def test_retired_mirror_pr_without_merged_origin_blocks(self):
        """PLAT-5854 fail-closed: open mirror PR with NO merged Origin PR still blocks."""
        item = _item(prs=[MIRROR_PR])
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata', return_value=(False, {}),
        ):
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService._validate_linked_pr_terminal_gate(item, 'done')
        self.assertIn('PLAT-5675', str(ctx.exception))

    def test_merged_at_implies_merged(self):
        """merged_at on a row with no status is treated as merged, not open."""
        item = _item(prs=[{
            'url': 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/341',
            'repo': 'shizuha-labs/deploy',
            'number': 341,
            'merged_at': '2026-08-28T00:00:00Z',
        }])
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata', return_value=(False, {}),
        ):
            WorkflowService._validate_linked_pr_terminal_gate(item, 'done')

    def test_legacy_string_entry_blocks(self):
        """Legacy string PR entries cannot prove terminal — fail closed."""
        item = _item(prs=['https://origin.shizuha.com/shizuha-labs/deploy/pulls/341'])
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata', return_value=(False, {}),
        ):
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService._validate_linked_pr_terminal_gate(item, 'done')
        self.assertIn('PLAT-5675', str(ctx.exception))
