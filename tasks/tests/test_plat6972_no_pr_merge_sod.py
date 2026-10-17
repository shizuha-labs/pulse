"""PLAT-6972: no-pr items must not be blocked by the merge SoD gate.

A `no-pr` item (e.g. a recurring no-pr hygiene sweep) has no PR and therefore
no merge SoD evidence by definition. Historical linked PRs from prior runs (or a
stale `merge_sod_hold` metadata left by a merge-lane reconciliation) must not
make the item permanently unroutable — the resolver must skip the merge SoD
evidence check for no-pr tasks so they stay routable to their owner team.
"""
from unittest import mock

from django.test import TestCase

from tasks.models import Item, Project
from tasks.services.workflow_service import WorkflowService


class RetiredMirrorMergeSodTests(TestCase):
    """SCLI-326: a stale evidence hold plus retired GitHub links must not
    refuse every merger. Author/reviewer separation still applies to a live
    Origin pull request.
    """

    def setUp(self):
        self.project = Project.objects.create(
            name='SCLI-326 SoD', key='S326', org_id=1, access_level='open',
        )

    def test_merged_and_github_mirror_links_do_not_block_a_merger(self):
        item = Item.objects.create(
            title='package the extension',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='in_progress',
            workflow_status='in_progress',
            assignment_group='merge',
            priority='urgent',
            metadata={
                'merge_sod_hold': {
                    'reason': 'sod_evidence_unavailable',
                    'current_pr_authors': [],
                    'current_head_reviewers': [],
                    'excluded_assignees': [
                        'aoi@shizuha.com', 'haru@shizuha.com',
                        'kei@shizuha.com', 'ryo@shizuha.com',
                    ],
                    'unknown_identity_assignees': [],
                },
                'pull_requests': [
                    {
                        'url': 'https://github.com/shizuha-labs/shizuha/pull/84',
                        'status': 'merged',
                    },
                    {
                        'url': 'https://github.com/shizuha-labs/deploy/pull/423',
                        'status': 'open',
                    },
                    {
                        'url': 'https://origin.shizuha.com/shizuha-labs/shizuha/pulls/2',
                        'status': 'merged',
                    },
                ],
            },
        )
        with mock.patch('requests.get') as request_get:
            reasons = WorkflowService._merge_sod_candidate_reasons(
                item, 'ryo@shizuha.com',
            )
        self.assertEqual(reasons, ())
        request_get.assert_not_called()

    def test_live_origin_author_is_still_refused(self):
        item = Item.objects.create(
            title='open origin pull request',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            metadata={
                'pull_requests': [{
                    'url': 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/9',
                    'status': 'open',
                }],
            },
        )
        with mock.patch.object(
            WorkflowService, '_linked_origin_pr_authors',
            return_value=({'ryo'}, [], 1),
        ), mock.patch.object(
            WorkflowService, '_linked_pr_current_head_reviewers',
            return_value=(set(), [], 1),
        ), mock.patch.object(
            WorkflowService, '_origin_author_logins_match_email',
            return_value=True,
        ):
            reasons = WorkflowService._merge_sod_candidate_reasons(
                item, 'ryo@shizuha.com',
            )
        self.assertEqual(reasons, ('merge_sod_author_identity',))

    def test_stale_hold_identities_do_not_refuse_without_a_live_origin_pr(self):
        item = Item.objects.create(
            title='hold left over from a github 404',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            metadata={
                'merge_sod_hold': {
                    'reason': 'sod_evidence_unavailable',
                    'current_pr_authors': ['ryo'],
                    'current_head_reviewers': ['kei'],
                    'excluded_assignees': ['ryo@shizuha.com', 'kei@shizuha.com'],
                },
                'pull_requests': [{
                    'url': 'https://github.com/shizuha-labs/deploy/pull/423',
                    'status': 'open',
                }],
            },
        )
        with mock.patch('requests.get') as request_get:
            reasons = WorkflowService._merge_sod_candidate_reasons(
                item, 'kei@shizuha.com',
            )
        self.assertEqual(reasons, ())
        request_get.assert_not_called()


class NoPrMergeSodRegressionTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='PLAT-6972 Test', key='P6972', org_id=1,
            access_level='open',
        )

    def _make_no_pr_item(self, status='todo', with_hold=True):
        metadata = {}
        if with_hold:
            metadata['merge_sod_hold'] = {
                'reason': 'sod_evidence_unavailable',
                'current_pr_authors': ['hiro'],
                'current_head_reviewers': ['kai2574', 'rei'],
                'excluded_assignees': ['aoi@shizuha.com', 'haru@shizuha.com'],
            }
        # Historical linked PRs from prior runs (the recurring sweep has done
        # PR work before, but the current cycle is a no-pr hygiene pass).
        metadata['pull_requests'] = [
            {'url': 'https://origin.shizuha.com/shizuha-labs/shizuha-stack/pulls/28'},
        ]
        return Item.objects.create(
            title='no-pr recurring hygiene sweep',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status=status,
            workflow_status=status,
            assignment_group='architecture',
            labels=['no-pr'],
            metadata=metadata,
        )

    def test_no_pr_item_skips_merge_sod_gate(self):
        """A no-pr item with a stale merge_sod_hold must not deny candidates."""
        item = self._make_no_pr_item()
        with mock.patch.object(
            WorkflowService,
            '_linked_origin_pr_authors',
            return_value=({'hiro'}, [], 1),
        ), mock.patch.object(
            WorkflowService,
            '_linked_pr_current_head_reviewers',
            return_value=({'kai2574', 'rei'}, [], 1),
        ), mock.patch.object(
            WorkflowService,
            '_linked_pr_entries',
            return_value=[
                {'url': 'https://origin.shizuha.com/shizuha-labs/shizuha-stack/pulls/28',
                 'status': 'open'},
            ],
        ):
            # Without the fix, the stale hold + linked PRs would deny every
            # candidate with merge_sod_evidence_unavailable. With the fix, the
            # no-pr label short-circuits the gate entirely.
            for email in ('hiro@shizuha.com', 'sora@shizuha.com', 'kumo@shizuha.com'):
                reasons = WorkflowService._merge_sod_candidate_reasons(item, email)
                self.assertEqual(
                    reasons, (),
                    f'{email} should have no merge SoD denial on a no-pr item, got {reasons}',
                )

    def test_no_pr_item_routable_via_team_resolver(self):
        """A no-pr recurring item's seat verdict carries no merge SoD denial."""
        item = self._make_no_pr_item()
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_assignment', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_agent_active_load', return_value=0,
        ), mock.patch.object(
            WorkflowService, '_agent_workable_load', return_value=0,
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_task_required_capabilities', return_value=set(),
        ), mock.patch.object(
            WorkflowService, '_linked_origin_pr_authors',
            return_value=({'hiro'}, [], 1),
        ), mock.patch.object(
            WorkflowService, '_linked_pr_current_head_reviewers',
            return_value=({'kai2574', 'rei'}, [], 1),
        ), mock.patch.object(
            WorkflowService, '_linked_pr_entries',
            return_value=[
                {'url': 'https://origin.shizuha.com/shizuha-labs/shizuha-stack/pulls/28',
                 'status': 'open'},
            ],
        ):
            verdict = WorkflowService._seat_routability_verdict(
                item, 'architecture', 'hiro@shizuha.com',
            )
        self.assertTrue(
            verdict.ok,
            f'no-pr recurring item must be routable; reasons={verdict.reasons}',
        )
        self.assertNotIn('merge_sod_evidence_unavailable', verdict.reasons)

    def test_non_no_pr_item_still_enforces_gate(self):
        """A normal (PR-carrying) item with a hold still enforces merge SoD."""
        item = self._make_no_pr_item(with_hold=True)
        item.labels = []  # remove the no-pr label — gate must apply again
        item.save(update_fields=['labels'])
        with mock.patch.object(
            WorkflowService,
            '_linked_origin_pr_authors',
            return_value=({'hiro'}, [], 1),
        ), mock.patch.object(
            WorkflowService,
            '_linked_pr_current_head_reviewers',
            return_value=({'kai2574', 'rei'}, [], 1),
        ), mock.patch.object(
            WorkflowService,
            '_linked_pr_entries',
            return_value=[
                {'url': 'https://origin.shizuha.com/shizuha-labs/shizuha-stack/pulls/28',
                 'status': 'open'},
            ],
        ), mock.patch.object(
            WorkflowService, '_agent_github_identity', return_value='some-login',
        ):
            reasons = WorkflowService._merge_sod_candidate_reasons(
                item, 'hiro@shizuha.com',
            )
        # Without the no-pr label the gate applies again — the author seat is
        # denied (merge_sod_author_identity) and the item is not routable.
        self.assertNotEqual(reasons, ())
        self.assertIn('merge_sod_author_identity', reasons)
