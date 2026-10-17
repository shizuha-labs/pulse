"""
PLAT-4151 regression: the cancel guard's fix-evidence comment scan must not
block cancel on a bare everyday keyword.

An operator commented "false alarm, was a gpu issue and the kernel hang got
fixed by reboot" on an invalid task — the old guard matched the substring
"fixed" and refused Cancel forever. Fix-evidence now requires the keyword to
co-occur with a concrete PR/commit reference, and explicit false-alarm
comments never count.
"""

from django.test import TestCase

from tasks.models import Comment, Item, ItemActivity
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


def _make_item(**overrides):
    defaults = dict(
        mode='task',
        title='PLAT-4151 guard test item',
        workflow_name='simple',
        status='open',
    )
    defaults.update(overrides)
    return Item.objects.create(**defaults)


CANCEL_SCREEN = {'reason_category': 'invalid'}


class CancelFixEvidenceGuardTests(TestCase):

    def _comment(self, item, content):
        return Comment.objects.create(
            item=item,
            author_id=1,
            author_email='plat4151@example.com',
            content=content,
        )

    def test_bare_keyword_in_false_alarm_comment_allows_cancel(self):
        """The PLAT-4151 case verbatim: 'fixed by reboot' is not fix evidence."""
        item = _make_item()
        self._comment(item, 'false alarm, was a gpu issue and the kernel hang got fixed by reboot')
        # Must not raise.
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_bare_keyword_without_artifact_allows_cancel(self):
        """Even without a false-alarm marker, a keyword with no PR/commit ref passes."""
        item = _make_item()
        self._comment(item, 'this got fixed on its own after restart')
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_unlinked_pr_link_is_not_owned_fix_evidence(self):
        item = _make_item()
        self._comment(item, 'fixed and merged in https://github.com/shizuha-labs/pulse/pull/481')
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_unlinked_pr_number_is_not_owned_fix_evidence(self):
        item = _make_item()
        self._comment(item, 'deployed via PR #123, verified on prod')
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_keyword_with_uncorroborated_commit_sha_allows_cancel(self):
        """PLS-882: keyword+SHA without merged delivery must not trap Cancel."""
        item = _make_item()
        self._comment(item, 'fixed in commit b28100dcafe')
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_pls882_unmerged_feature_branch_commit_allows_cancel(self):
        """PLAT-4560 shape: prose 'shipped <feature-branch SHA>' is not delivery."""
        unmerged = '8bb8829e9d1a6fb258a425d7e66a6a05d8c0c21b'
        item = _make_item(
            status='open',
            metadata={'pull_requests': [
                {
                    'url': 'https://github.com/shizuha-labs/hive/pull/322',
                    'status': 'closed',
                },
                {
                    'url': 'https://origin.shizuha.com/shizuha-labs/hive/pulls/327',
                    'status': 'closed',
                    'head_sha': unmerged,
                },
            ]},
        )
        self._comment(
            item,
            f"shipped {unmerged} on fix/plat4560-sara-identity — never merged to main",
        )
        WorkflowService._validate_cancel_preconditions(
            item,
            {'reason_category': 'obsolete', 'comment': 'ORIG-18 retired GitHub control plane'},
        )

    def test_pls882_comment_sha_matching_merged_origin_head_blocks_via_pr_guard(self):
        """Negative control: merged Origin PR still protects Cancel (section a)."""
        delivered = 'c0ffee1234567890abcdef'
        item = _make_item(metadata={'pull_requests': [
            {
                'url': 'https://origin.shizuha.com/shizuha-labs/x/pulls/42',
                'status': 'merged',
                'head_sha': delivered,
            },
        ]})
        self._comment(item, f'shipped commit {delivered[:12]}')
        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)
        self.assertIn('active linked PR', str(caught.exception))

    def test_pls882_comment_sha_matching_deploy_event_blocks_cancel(self):
        """PLS-882: keyword+SHA corroborated by prod DeploymentEvent blocks Cancel."""
        from tasks.models import DeploymentEvent

        delivered = 'deadbeefcafebabe0123456789abcdef01234567'
        item = _make_item()
        Item.objects.filter(pk=item.pk).update(item_key='PLS-882-TEST')
        item.refresh_from_db()
        DeploymentEvent.objects.create(
            idempotency_key=f'pls882-{item.pk}-{delivered[:12]}',
            repo='shizuha-labs/pulse',
            service='pulse',
            environment='live',
            cluster='shizuha-k3s',
            commit_sha=delivered,
            state='smoke_passed',
            smoke_result='passed',
            source_task_keys=[item.item_key],
        )
        comment = self._comment(item, f'deployed commit {delivered[:12]} to prod')
        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)
        message = str(caught.exception)
        self.assertIn(f'id={comment.id}', message)
        self.assertIn('commit evidence', message)

    def test_negated_or_false_valued_keyword_with_commit_allows_cancel(self):
        for report in (
            'not merged in commit b28100dcafe',
            'un-merged commit b28100dcafe',
            'never deployed; commit b28100dcafe is only a candidate',
            'merged FALSE — head commit b28100dcafe',
            'verified: no; commit b28100dcafe is unrelated',
            'nobody has shipped commit b28100dcafe',
        ):
            with self.subTest(report=report):
                item = _make_item()
                self._comment(item, report)
                WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_mergeable_is_not_merged_keyword(self):
        item = _make_item()
        self._comment(item, 'PR is open and mergeable; head b28100dcafe')
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_bare_eight_hex_wiki_id_is_not_commit_evidence(self):
        item = _make_item()
        self._comment(item, 'fixed guidance lives on wiki page d3fe77e7')
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_real_triage_state_report_allows_cancel(self):
        item = _make_item()
        self._comment(
            item,
            'hive#47 state=closed merged=FALSE mergeable=True; '
            'per 27cab457, see deploy#115 (merged) for the sibling case',
        )
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_deployment_event_uuid_is_not_commit_evidence(self):
        """Generated watchlist context must not masquerade as a fix commit."""
        item = _make_item(item_type='task.regression_watchlist')
        self._comment(
            item,
            'Auto-created from DeploymentEvent '
            '`b68a9021-bfca-4ed8-998c-5a4ab217c947`: recently-fixed '
            'surfaces need re-verification.',
        )
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_false_alarm_marker_overrides_artifact_evidence(self):
        """An explicit false-alarm declaration wins even if a sha appears in prose."""
        item = _make_item()
        self._comment(item, 'false alarm — the deploy b28100dcafe everyone blamed was unrelated')
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_linked_pr_metadata_still_blocks_cancel(self):
        """Check (a) is untouched: active PR in metadata blocks regardless of comments."""
        item = _make_item(metadata={'pull_requests': [{'url': 'https://github.com/x/y/pull/1', 'status': 'open'}]})
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_legacy_string_pr_blocks_cancel(self):
        """Legacy string-format PR entries (no status) always block cancel."""
        item = _make_item(metadata={'pull_requests': ['https://github.com/x/y/pull/1']})
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_all_closed_prs_allows_cancel(self):
        """PLS-440: when every linked PR is closed (not merged), cancel is allowed."""
        item = _make_item(metadata={'pull_requests': [
            {'url': 'https://github.com/x/y/pull/1', 'status': 'closed'},
            {'url': 'https://github.com/x/y/pull/2', 'status': 'closed'},
        ]})
        # Must not raise.
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_mixed_closed_and_github_merged_prs_allows_cancel(self):
        """PLS-440 + ORIG-18: GitHub-mirror merged is not Origin delivery."""
        item = _make_item(metadata={'pull_requests': [
            {'url': 'https://github.com/x/y/pull/1', 'status': 'closed'},
            {'url': 'https://github.com/x/y/pull/2', 'status': 'merged'},
        ]})
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_merged_origin_pr_blocks_cancel(self):
        """PLS-765: a genuinely merged Origin PR protects from Cancel."""
        item = _make_item(metadata={'pull_requests': [
            {'url': 'https://origin.shizuha.com/shizuha-labs/x/pulls/2', 'status': 'merged'},
        ]})
        self._comment(item, 'fixed and merged in https://origin.shizuha.com/shizuha-labs/x/pulls/2')
        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)
        self.assertIn('active linked PR', str(caught.exception))

    def test_obsolete_with_prior_awaiting_merge_and_closed_github_allows_cancel(self):
        """PLS-765 / PLAT-4560 shape: obsolete + historical awaiting_merge + frozen GH PRs."""
        item = _make_item(
            status='open',
            metadata={'pull_requests': [
                {'url': 'https://github.com/shizuha-labs/hive/pull/322', 'status': 'closed'},
            ]},
        )
        ItemActivity.objects.create(
            item=item,
            action='status_changed',
            field_name='Status',
            old_value='in_review',
            new_value='awaiting_merge',
            user_id=1,
            user_email='author@example.com',
        )
        WorkflowService._validate_cancel_preconditions(
            item, {'reason_category': 'obsolete', 'comment': 'ORIG-18 retired GitHub control plane'},
        )

    def test_current_in_review_blocks_cancel_without_pr_or_commit_evidence(self):
        """PLS-576: structured current state outranks wiki-verdict prose."""
        item = _make_item()
        # The small ``simple`` fixture workflow does not expose in_review and
        # Item.save() correctly coerces invalid slugs.  Persist the already-
        # transitioned lifecycle state directly, as the production row has it.
        Item.objects.filter(pk=item.pk).update(status='in_review')
        item.refresh_from_db()
        self._comment(
            item,
            'Verdict: PASS — canonical HLD 8a7e3fdb-5f41-4e31-a6e2-4ab2ae5bd910 '
            'is verified at wiki Version 4; P1=0/P2=0.',
        )

        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

        self.assertIn("currently in 'in_review'", str(caught.exception))

    def test_status_activity_history_alone_allows_cancel_without_merged_origin_fix(self):
        """PLS-765: historical review-lane visit is not delivery evidence by itself."""
        item = _make_item(status='open')
        ItemActivity.objects.create(
            item=item,
            action='status_changed',
            field_name='Status',
            old_value='In Progress',
            new_value='In Review',
            user_id=1,
            user_email='reviewer@example.com',
        )
        # Must not raise — no merged Origin PR.
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_status_history_plus_merged_origin_pr_blocks_cancel(self):
        """PLS-765 negative control: history + merged Origin PR = real delivery."""
        item = _make_item(
            status='open',
            metadata={'pull_requests': [
                {
                    'url': 'https://origin.shizuha.com/shizuha-labs/x/pulls/9',
                    'status': 'merged',
                    'head_sha': 'abc1234',
                },
            ]},
        )
        activity = ItemActivity.objects.create(
            item=item,
            action='status_changed',
            field_name='Status',
            old_value='open',
            new_value='awaiting_merge',
            user_id=1,
            user_email='reviewer@example.com',
        )
        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService._validate_cancel_preconditions(
                item, {'reason_category': 'obsolete'},
            )
        msg = str(caught.exception)
        # Either the merged-Origin PR check or the history+merged path.
        self.assertTrue(
            'merged Origin' in msg or 'active linked PR' in msg or f'id={activity.id}' in msg,
            msg=msg,
        )

    def test_early_invalid_item_with_wiki_verdict_prose_allows_cancel(self):
        """Verdict/wiki prose alone is not authority for a cancel block."""
        item = _make_item(status='open')
        self._comment(
            item,
            'Quoted review note: Verdict: PASS — page '
            '8a7e3fdb-5f41-4e31-a6e2-4ab2ae5bd910 at wiki Version 4; P1=0/P2=0.',
        )

        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_mixed_active_and_closed_prs_blocks_cancel(self):
        """PLS-440: any open/active PR still blocks cancel."""
        item = _make_item(metadata={'pull_requests': [
            {'url': 'https://github.com/x/y/pull/1', 'status': 'closed'},
            {'url': 'https://github.com/x/y/pull/2', 'status': 'open'},
        ]})
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_reason_category_still_required(self):
        """Check (1) is untouched: no category, no cancel."""
        item = _make_item()
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_cancel_preconditions(item, {'comment': 'cleanup'})


class RetiredMirrorGithubPrCancelTests(TestCase):
    """PLAT-4686: retired-mirror exemption is provenance-gated."""

    def test_github_only_with_obsolete_disposition_allows_cancel(self):
        item = _make_item(metadata={'pull_requests': [
            {'url': 'https://github.com/shizuha-labs/hive/pull/322', 'status': 'open'},
        ]})
        WorkflowService._validate_cancel_preconditions(
            item, {'reason_category': 'obsolete'},
        )

    def test_bare_github_only_without_obsolete_disposition_blocks(self):
        item = _make_item(metadata={'pull_requests': [
            'https://github.com/shizuha-labs/hive/pull/322',
        ]})
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_github_with_provenanced_terminal_origin_replacement_allows_cancel(self):
        item = _make_item(metadata={'pull_requests': [
            {'url': 'https://github.com/shizuha-labs/hive/pull/322', 'status': 'open'},
            {
                'url': 'https://origin.shizuha.com/shizuha-labs/hive/pulls/9',
                'status': 'closed',
                'head_sha': 'replacement-head',
            },
        ]})
        WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_github_with_provenance_free_origin_replacement_blocks(self):
        item = _make_item(metadata={'pull_requests': [
            {'url': 'https://github.com/shizuha-labs/hive/pull/322', 'status': 'open'},
            {
                'url': 'https://origin.shizuha.com/shizuha-labs/hive/pulls/9',
                'status': 'closed',
            },
        ]})
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_origin_open_pr_still_blocks_cancel(self):
        item = _make_item(metadata={'pull_requests': [
            {'url': 'https://origin.shizuha.com/shizuha-labs/hive/pulls/9', 'status': 'open'},
        ]})
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_github_open_plus_origin_open_still_blocks(self):
        item = _make_item(metadata={'pull_requests': [
            {'url': 'https://github.com/shizuha-labs/hive/pull/322', 'status': 'open'},
            {
                'url': 'https://origin.shizuha.com/shizuha-labs/hive/pulls/9',
                'status': 'open',
                'head_sha': 'active-origin-head',
            },
        ]})
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_cancel_preconditions(item, CANCEL_SCREEN)

    def test_lookalike_github_host_remains_blocking(self):
        item = _make_item(metadata={'pull_requests': [
            {
                'url': 'https://github.com.evil.example/shizuha-labs/hive/pull/322',
                'status': 'open',
            },
        ]})
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_cancel_preconditions(
                item, {'reason_category': 'obsolete'},
            )

    def test_external_github_owner_remains_blocking(self):
        item = _make_item(metadata={'pull_requests': [
            {
                'url': 'https://github.com/external-org/hive/pull/322',
                'status': 'open',
            },
        ]})
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_cancel_preconditions(
                item, {'reason_category': 'obsolete'},
            )

    def test_http_github_url_remains_blocking(self):
        item = _make_item(metadata={'pull_requests': [
            {
                'url': 'http://github.com/shizuha-labs/hive/pull/322',
                'status': 'open',
            },
        ]})
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._validate_cancel_preconditions(
                item, {'reason_category': 'obsolete'},
            )
