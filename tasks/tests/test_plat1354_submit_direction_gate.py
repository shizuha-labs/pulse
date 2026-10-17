"""PLAT-1354: submit-for-review blocks unread newer reviewer direction."""
import hashlib
import inspect
from io import StringIO
import itertools
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.test import TestCase
from django.utils import timezone

from tasks.models import (
    Comment,
    Item,
    ItemActivity,
    ItemLink,
    Project,
    ProjectMembership,
    ProjectTeamLead,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.comment_writer_guard import (
    assert_no_direct_comment_writer_drift,
    direct_comment_writer_inventory,
)
from tasks.serializers import CommentCreateSerializer, CommentSerializer
from tasks.services.mechanical_comments import MechanicalCommentFactory, MECHANICAL_COMMENT_SPECS
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


_key_counter = itertools.count(1)
PR_URL = 'https://github.com/shizuha-labs/shizuha-tasks/pull/1354'
ORIGIN_PR_URL = 'https://origin.shizuha.com/shizuha-labs/hive/pulls/88'


def _transition():
    return SimpleNamespace(
        validators=[{'type': 'submit_requires_current_direction'}],
        from_status='in_progress',
        to_status='in_review',
    )


def _item(*, head_at=None, head_sha='abc123', project=None):
    if head_at is None:
        head_at = timezone.now()
    return Item.objects.create(
        item_key=f'PLAT-1354-T{next(_key_counter)}',
        title='submit direction freshness test',
        project=project,
        status='in_progress',
        workflow_name='autonomous-dev',
        workflow_status='in_progress',
        assignee_id=101,
        assignee_email='author@agents.shizuha.io',
        created_by_id=101,
        created_by_email='author@agents.shizuha.io',
        metadata={
            'pull_requests': [{
                'url': PR_URL,
                'repo': 'shizuha-labs/shizuha-tasks',
                'number': 1354,
                'status': 'open',
                'head_sha': head_sha,
                'last_synced_at': head_at.isoformat(),
            }],
            'implementer': {'id': 101, 'email': 'author@agents.shizuha.io'},
        },
    )


def _comment(item, email, body, created_at):
    c = Comment.objects.create(
        item=item,
        author_id=202,
        author_email=email,
        content=body,
    )
    Comment.objects.filter(pk=c.pk).update(created_at=created_at, updated_at=created_at)
    c.refresh_from_db()
    return c


class SubmitRequiresCurrentDirectionTests(TestCase):
    def setUp(self):
        TeamRoster.objects.update_or_create(
            team_name='review',
            defaults={
                'tiers': [['revi@shizuha.com']],
                'junior_emails': [],
                'is_active': True,
                'note': 'PLAT-1354 test roster',
            },
        )
        TeamRoster.objects.update_or_create(
            team_name='architecture',
            defaults={
                'tiers': [['sora@shizuha.com']],
                'junior_emails': [],
                'is_active': True,
                'note': 'PLAT-1354 test roster',
            },
        )
        WorkflowService._roster_cache = None

    def test_newer_reviewer_comment_blocks_submit_and_names_comment(self):
        head_at = timezone.now() - timedelta(minutes=10)
        item = _item(head_at=head_at)
        _comment(item, 'revi@shizuha.com', 'P1: read this before resubmitting.', head_at + timedelta(minutes=1))

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(_transition(), item)

        msg = str(ctx.exception)
        self.assertIn('Submit-for-review direction gate', msg)
        self.assertIn('revi@shizuha.com', msg)
        self.assertIn('P1: read this before resubmitting.', msg)

    def test_author_ack_after_latest_direction_allows_submit(self):
        head_at = timezone.now() - timedelta(minutes=10)
        item = _item(head_at=head_at)
        direction_at = head_at + timedelta(minutes=1)
        _comment(item, 'sora@shizuha.com', 'Architect ruling: use the webhook provenance.', direction_at)
        _comment(item, 'author@agents.shizuha.io', 'ack: read and incorporated the architect ruling.', direction_at + timedelta(minutes=1))

        WorkflowService._run_validators(_transition(), item)

    def test_author_on_review_roster_does_not_self_block_with_newer_comment(self):
        """PLAT-4500: an implementer/reviewer dual-role comment is not direction."""
        head_at = timezone.now() - timedelta(minutes=10)
        item = _item(head_at=head_at)
        item.assignee_email = 'revi@shizuha.com'
        item.save(update_fields=['assignee_email'])
        _comment(
            item,
            'revi@shizuha.com',
            '## Invariant sketch\nAuthor pre-sync for the submitted head.',
            head_at + timedelta(minutes=1),
        )

        WorkflowService._run_validators(_transition(), item)

    def test_elected_project_lead_direction_uses_project_key_and_org(self):
        """The lead lookup follows ProjectTeamLead's real composite key."""
        head_at = timezone.now() - timedelta(minutes=10)
        project = Project.objects.create(org_id=2, key='P1354', name='Direction gate')
        ProjectTeamLead.objects.create(
            org_id=2,
            project_key='P1354',
            team_name='engineering',
            lead_email='lead@shizuha.com',
        )
        ProjectMembership.objects.create(
            project=project,
            user_id=101,
            user_email='author@agents.shizuha.io',
            is_active=True,
        )
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            item = _item(head_at=head_at, project=project)
        _comment(item, 'lead@shizuha.com', 'Lead ruling: update the proof.', head_at + timedelta(minutes=1))

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(_transition(), item)
        self.assertIn('lead@shizuha.com', str(ctx.exception))

    def test_head_advance_after_direction_allows_submit(self):
        head_at = timezone.now() - timedelta(minutes=10)
        item = _item(head_at=head_at)
        _comment(item, 'revi@shizuha.com', 'P1: fix this.', head_at + timedelta(minutes=1))
        meta = item.metadata
        meta['pull_requests'][0]['head_sha'] = 'def456'
        meta['pull_requests'][0]['last_synced_at'] = (head_at + timedelta(minutes=5)).isoformat()
        item.metadata = meta
        item.save(update_fields=['metadata'])

        WorkflowService._run_validators(_transition(), item)

    def test_non_authoritative_comment_does_not_block(self):
        head_at = timezone.now() - timedelta(minutes=10)
        item = _item(head_at=head_at)
        _comment(item, 'peer@agents.shizuha.io', 'Suggestion: maybe change this.', head_at + timedelta(minutes=1))

        WorkflowService._run_validators(_transition(), item)

    def test_missing_head_timestamp_fails_open_for_legacy_pr_metadata(self):
        item = _item()
        meta = item.metadata
        meta['pull_requests'][0].pop('last_synced_at')
        item.metadata = meta
        item.save(update_fields=['metadata'])
        _comment(item, 'revi@shizuha.com', 'P1: newer direction.', timezone.now())

        with self.assertLogs('tasks.services.workflow_service', level='WARNING') as logs:
            WorkflowService._run_validators(_transition(), item)
        self.assertIn('missing head timestamp', '\n'.join(logs.output))


class PLS895TerminalReviewRecoveryTests(TestCase):
    """Production-order coverage for terminal recovery at an unchanged head.

    The real PLAT-4719 sequence was terminal task -> newer recovery direction ->
    failed Reopen -> non-author recovery ack/rebuttal -> failed Reopen.  The
    successful retry must restore the Review lane and re-arm the existing
    blocker link, while the same non-author comment remains insufficient for an
    ordinary submit.
    """

    def setUp(self):
        self.actor = SimpleNamespace(
            id=404,
            email='ni@shizuha.com',
            is_staff=True,
            is_superuser=True,
            is_authenticated=True,
        )
        TeamRoster.objects.update_or_create(
            team_name='review',
            defaults={
                'tiers': [['revi@shizuha.com']],
                'junior_emails': [],
                'is_active': True,
                'note': 'PLS-895 review roster',
            },
        )
        TeamRoster.objects.update_or_create(
            team_name='architecture',
            defaults={
                'tiers': [['aoi@shizuha.com']],
                'junior_emails': [],
                'is_active': True,
                'note': 'PLS-895 architecture roster',
            },
        )
        TeamRoster.objects.update_or_create(
            team_name='qa',
            defaults={
                'tiers': [[self.actor.email]],
                'junior_emails': [],
                'is_active': True,
                'note': 'PLS-895 recovery actor is also authoritative QA',
            },
        )
        WorkflowService._roster_cache = None

        statuses = {}
        for slug, name, category in (
            ('in_progress', 'In Progress', 'in_progress'),
            ('in_review', 'In Review', 'in_progress'),
            ('blocked', 'Blocked', 'in_progress'),
            ('done', 'Done', 'done'),
        ):
            statuses[slug], _ = Status.objects.get_or_create(
                slug=slug,
                defaults={'name': name, 'category': category},
            )
        self.workflow = Workflow.objects.create(
            name='PLS 895 terminal review recovery',
            slug=f'pls895-terminal-recovery-{next(_key_counter)}',
            initial_status=statuses['in_progress'],
        )
        for position, status in enumerate(statuses.values()):
            WorkflowStatus.objects.create(
                workflow=self.workflow,
                status=status,
                position=position,
            )
        self.reopen = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Reopen for Review',
            from_status='done',
            to_status='in_review',
            validators=[
                {'type': 'pr_linked_or_flagged'},
                {'type': 'submit_requires_current_direction'},
            ],
            post_functions=[
                {'type': 'clear_resolution'},
                {'type': 'assign_team', 'value': 'review'},
            ],
            position=0,
        )
        self.submit = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Submit for Code Review',
            from_status='in_progress',
            to_status='in_review',
            validators=[{'type': 'submit_requires_current_direction'}],
            position=1,
        )
        self.block = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Blocked by Linked Issue',
            from_status='in_progress',
            to_status='blocked',
            position=2,
        )

    def _workflow_item(self, *, status, head_at, key_prefix):
        item = _item(head_at=head_at)
        item.item_key = f'{key_prefix}-{next(_key_counter)}'
        item.status = status
        item.workflow_status = status
        item.workflow_name = self.workflow.slug
        item.assignment_group = 'merge' if status == 'done' else 'engineering'
        item.assignee_id = 303 if status == 'done' else 101
        item.assignee_email = (
            'kei@shizuha.com'
            if status == 'done'
            else 'author@agents.shizuha.io'
        )
        item.save(update_fields=[
            'item_key', 'status', 'workflow_status', 'workflow_name',
            'assignment_group', 'assignee_id', 'assignee_email',
        ])
        return item

    def test_recovery_actor_ack_reopens_and_reblocks_existing_dependent(self):
        head_at = timezone.now() - timedelta(minutes=10)
        blocker = self._workflow_item(
            status='done', head_at=head_at, key_prefix='PLS-895-BLOCKER',
        )
        dependent = self._workflow_item(
            status='in_progress', head_at=head_at,
            key_prefix='PLS-895-DEPENDENT',
        )
        link = ItemLink.objects.create(
            source_item=blocker,
            target_item=dependent,
            link_type='blocks',
            created_by_id=self.actor.id,
            created_by_email=self.actor.email,
        )
        dependent.refresh_from_db()
        self.assertEqual(dependent.status, 'in_progress')

        direction_at = head_at + timedelta(minutes=1)
        _comment(
            blocker,
            'aoi@shizuha.com',
            'Recovery direction: restore the already-reviewed head to Merge.',
            direction_at,
        )

        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                blocker,
                'in_review',
                user=self.actor,
                transition_id=str(self.reopen.pk),
            )
        blocker.refresh_from_db()
        self.assertEqual(blocker.status, 'done')

        _comment(
            blocker,
            self.actor.email,
            'ack: read the recovery direction. PLAT-202 rebuttal: the reviewed '
            'unchanged head needs its Merge/CI lane restored, not a fake delta.',
            direction_at + timedelta(minutes=1),
        )

        WorkflowService.execute_transition(
            blocker,
            'in_review',
            user=self.actor,
            transition_id=str(self.reopen.pk),
        )

        blocker.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(blocker.status, 'in_review')
        self.assertEqual(blocker.assignment_group, 'review')
        self.assertNotEqual(blocker.assignee_email, self.actor.email)
        self.assertEqual(dependent.status, 'blocked')
        self.assertTrue(ItemLink.objects.filter(pk=link.pk).exists())

    def test_recovery_actor_ack_does_not_bypass_ordinary_submit(self):
        head_at = timezone.now() - timedelta(minutes=10)
        item = self._workflow_item(
            status='in_progress', head_at=head_at,
            key_prefix='PLS-895-ORDINARY',
        )
        direction_at = head_at + timedelta(minutes=1)
        _comment(
            item,
            'aoi@shizuha.com',
            'Architecture direction: ordinary submit still needs author action.',
            direction_at,
        )
        _comment(
            item,
            self.actor.email,
            'ack: a recovery actor cannot acknowledge an ordinary author submit.',
            direction_at + timedelta(minutes=1),
        )

        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                item,
                'in_review',
                user=self.actor,
                transition_id=str(self.submit.pk),
            )

    def test_later_peer_direction_after_recovery_ack_still_blocks(self):
        head_at = timezone.now() - timedelta(minutes=10)
        item = self._workflow_item(
            status='done', head_at=head_at,
            key_prefix='PLS-895-LATER-PEER',
        )
        direction_at = head_at + timedelta(minutes=1)
        _comment(
            item,
            'aoi@shizuha.com',
            'Architecture direction: confirm the recovery boundary.',
            direction_at,
        )
        _comment(
            item,
            self.actor.email,
            'ack: recovery direction read and understood.',
            direction_at + timedelta(minutes=1),
        )
        _comment(
            item,
            'revi@shizuha.com',
            'Review direction: a newer issue still needs author action.',
            direction_at + timedelta(minutes=2),
        )

        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                item,
                'in_review',
                user=self.actor,
                transition_id=str(self.reopen.pk),
            )
        item.refresh_from_db()
        self.assertEqual(item.status, 'done')

    def test_recovery_actor_non_marker_direction_still_blocks(self):
        head_at = timezone.now() - timedelta(minutes=10)
        item = self._workflow_item(
            status='done', head_at=head_at,
            key_prefix='PLS-895-ACTOR-DIRECTION',
        )
        direction_at = head_at + timedelta(minutes=1)
        _comment(
            item,
            'aoi@shizuha.com',
            'Architecture direction: confirm the recovery boundary.',
            direction_at,
        )
        _comment(
            item,
            self.actor.email,
            'PLAT-202 rebuttal: the unchanged reviewed head can recover.',
            direction_at + timedelta(minutes=1),
        )
        _comment(
            item,
            self.actor.email,
            'QA direction: stop; the live recovery evidence is incomplete.',
            direction_at + timedelta(minutes=2),
        )

        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                item,
                'in_review',
                user=self.actor,
                transition_id=str(self.reopen.pk),
            )
        item.refresh_from_db()
        self.assertEqual(item.status, 'done')


class PLS477ArchitectAssigneeAckTests(TestCase):
    """PLS-477: assignee who is themselves a direction-team member must be able to
    clear a newer lead approval with an ack/rebuttal without it self-renewing."""

    def setUp(self):
        TeamRoster.objects.update_or_create(
            team_name='architecture',
            defaults={'tiers': [['sora@shizuha.com', 'aoi@shizuha.com']],
                      'junior_emails': [], 'is_active': True, 'note': 'PLS-477 roster'},
        )
        TeamRoster.objects.update_or_create(
            team_name='review',
            defaults={'tiers': [['revi@shizuha.com']],
                      'junior_emails': [], 'is_active': True, 'note': 'PLS-477 roster'},
        )
        WorkflowService._roster_cache = None

    def _architect_item(self, head_at):
        item = _item(head_at=head_at)
        item.assignee_email = 'sora@shizuha.com'
        item.created_by_email = 'sora@shizuha.com'
        meta = item.metadata
        meta['implementer'] = {'id': 101, 'email': 'sora@shizuha.com'}
        item.metadata = meta
        item.save(update_fields=['assignee_email', 'created_by_email', 'metadata'])
        return item

    def test_architect_assignee_ack_clears_lead_approval(self):
        head_at = timezone.now() - timedelta(minutes=10)
        item = self._architect_item(head_at)
        _comment(item, 'aoi@shizuha.com', 'Architecture approval / co-sign: zero findings at this exact head.', head_at + timedelta(minutes=1))
        _comment(item, 'sora@shizuha.com', 'ack: lead approval noted; no code change requested.', head_at + timedelta(minutes=2))
        WorkflowService._run_validators(_transition(), item)  # must NOT raise

    def test_architect_assignee_rebuttal_clears_and_does_not_self_renew(self):
        head_at = timezone.now() - timedelta(minutes=10)
        item = self._architect_item(head_at)
        _comment(item, 'aoi@shizuha.com', 'Please double-check the provenance handling.', head_at + timedelta(minutes=1))
        _comment(item, 'sora@shizuha.com', 'PLAT-202 rebuttal: no change needed; provenance is already handled at this head.', head_at + timedelta(minutes=2))
        WorkflowService._run_validators(_transition(), item)  # must NOT raise (ack/rebuttal by assignee does not re-arm)

    def test_peer_reviewer_change_request_still_blocks_architect_assignee(self):
        head_at = timezone.now() - timedelta(minutes=10)
        item = self._architect_item(head_at)
        _comment(item, 'revi@shizuha.com', 'P1: this must change before merge.', head_at + timedelta(minutes=1))
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(_transition(), item)  # real newer direction from ANOTHER seat stays fail-closed

    def test_architect_assignee_self_cosign_and_ack_clears(self):
        """DOJO-15 exact case: the architect-assignee posts their OWN approval/co-sign
        + ack + rebuttal at the exact reviewed head. All are the assignee's own comments
        and must not count as newer direction against themselves (self-renewing defect)."""
        head_at = timezone.now() - timedelta(minutes=10)
        item = self._architect_item(head_at)  # assignee = sora (architecture roster seat)
        _comment(item, 'sora@shizuha.com', '## Architecture source-contract review — APPROVED. Zero blocking findings at this exact head.', head_at + timedelta(minutes=1))
        _comment(item, 'sora@shizuha.com', 'ack: read and applied; a review/co-sign of this exact existing head, not a requested code change.', head_at + timedelta(minutes=2))
        _comment(item, 'sora@shizuha.com', 'PLAT-202 rebuttal: the only post-head direction is my own approval; no successor commit required.', head_at + timedelta(minutes=3))
        WorkflowService._run_validators(_transition(), item)  # must NOT raise


class PLS758LinkedCurrentAuthorAckTests(TestCase):
    """PLS-758: exact-head PR authors remain authors on group-owned work."""

    def setUp(self):
        TeamRoster.objects.update_or_create(
            team_name='architecture',
            defaults={'tiers': [['aoi@shizuha.com']], 'junior_emails': [],
                      'is_active': True, 'note': 'PLS-758 architecture roster'},
        )
        TeamRoster.objects.update_or_create(
            team_name='merge',
            defaults={'tiers': [['ryo@shizuha.com']], 'junior_emails': [],
                      'is_active': True, 'note': 'PLS-758 merge roster'},
        )
        WorkflowService._roster_cache = None

    def _group_owned_item(self, head_at):
        item = _item(head_at=head_at)
        item.assignee_id = None
        item.assignee_email = ''
        item.created_by_email = 'jun@shizuha.com'
        meta = item.metadata
        meta.pop('implementer', None)
        meta['pull_requests'] = [
            {
                'url': 'https://github.com/shizuha-labs/hive/pull/88',
                'status': 'open',
                'head_sha': 'retired-mirror-head',
                'last_synced_at': (head_at - timedelta(minutes=5)).isoformat(),
            },
            {
                'url': ORIGIN_PR_URL,
                'repo': 'shizuha-labs/hive',
                'number': 88,
                'status': 'open',
                'head_sha': 'current-origin-head',
                'last_synced_at': head_at.isoformat(),
            },
        ]
        item.metadata = meta
        item.save(update_fields=[
            'assignee_id', 'assignee_email', 'created_by_email', 'metadata',
        ])
        return item

    @patch(
        'tasks.user_service.user_service.get_user_by_username',
        return_value={'id': 202, 'username': 'ryo', 'email': 'ryo@shizuha.com'},
    )
    @patch.object(
        WorkflowService, '_origin_pr_author_login', return_value=('ryo', None),
    )
    def test_group_owned_current_author_ack_clears_without_self_renewal(
        self, author_lookup, _identity_lookup,
    ):
        """Production order: blocked submit, author ack, unchanged-head submit."""
        head_at = timezone.now() - timedelta(minutes=10)
        item = self._group_owned_item(head_at)
        direction_at = head_at + timedelta(minutes=1)
        _comment(
            item,
            'aoi@shizuha.com',
            'Architecture direction: preserve the fail-closed provenance boundary.',
            direction_at,
        )

        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(_transition(), item)

        _comment(
            item,
            'ryo@shizuha.com',
            'ack: incorporated the newest direction for all linked PRs.',
            direction_at + timedelta(minutes=1),
        )
        WorkflowService._run_validators(_transition(), item)

        self.assertTrue(author_lookup.called)
        self.assertEqual(author_lookup.call_args.args[0]['url'], ORIGIN_PR_URL)
        self.assertEqual(
            author_lookup.call_args.kwargs,
            {'require_exact_head': True},
        )

    @patch.object(
        WorkflowService,
        '_origin_pr_author_login',
        side_effect=lambda _entry, *, require_exact_head=False: (
            (None, 'origin_pr_head_mismatch')
            if require_exact_head
            else ('ryo', None)
        ),
    )
    def test_unknown_or_stale_author_provenance_still_fails_closed(
        self, _author_lookup,
    ):
        head_at = timezone.now() - timedelta(minutes=10)
        item = self._group_owned_item(head_at)
        direction_at = head_at + timedelta(minutes=1)
        _comment(item, 'aoi@shizuha.com', 'Architecture direction.', direction_at)
        _comment(
            item,
            'ryo@shizuha.com',
            'ack: this must not count without exact-head author provenance.',
            direction_at + timedelta(minutes=1),
        )

        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(_transition(), item)


class PLS478TypedLifecycleProvenanceTests(TestCase):
    def setUp(self):
        TeamRoster.objects.update_or_create(
            team_name='review',
            defaults={'tiers': [['revi@shizuha.com']], 'junior_emails': [],
                      'is_active': True, 'note': 'PLS-478 review roster'},
        )
        TeamRoster.objects.update_or_create(
            team_name='merge',
            defaults={'tiers': [['kei@shizuha.com']], 'junior_emails': [],
                      'is_active': True, 'note': 'PLS-478 merge roster'},
        )
        WorkflowService._roster_cache = None

    def _real_sequence_workflow(self):
        statuses = {}
        for slug, name, category in (
            ('pls478_in_progress', 'PLS 478 In Progress', 'in_progress'),
            ('pls478_in_review', 'PLS 478 In Review', 'in_progress'),
            ('blocked', 'Blocked', 'in_progress'),
            ('done', 'Done', 'done'),
        ):
            statuses[slug], _ = Status.objects.get_or_create(
                slug=slug, defaults={'name': name, 'category': category},
            )
        workflow = Workflow.objects.create(
            name='PLS 478 real caller sequence',
            slug='pls478-real-caller-sequence',
            initial_status=statuses['pls478_in_progress'],
        )
        for position, status in enumerate(statuses.values()):
            WorkflowStatus.objects.create(
                workflow=workflow, status=status, position=position,
            )
        transitions = {
            'submit': WorkflowTransition.objects.create(
                workflow=workflow,
                name='Submit for Code Review',
                from_status='pls478_in_progress',
                to_status='pls478_in_review',
                validators=[{'type': 'submit_requires_current_direction'}],
                position=0,
            ),
            'return': WorkflowTransition.objects.create(
                workflow=workflow,
                name='Request Changes',
                from_status='pls478_in_review',
                to_status='pls478_in_progress',
                position=1,
            ),
            'block': WorkflowTransition.objects.create(
                workflow=workflow,
                name='Blocked by Linked Issue',
                from_status='pls478_in_progress',
                to_status='blocked',
                position=2,
            ),
            'resume': WorkflowTransition.objects.create(
                workflow=workflow,
                name='Resume after Blocker',
                from_status='blocked',
                to_status='pls478_in_progress',
                position=3,
            ),
            'complete': WorkflowTransition.objects.create(
                workflow=workflow,
                name='Complete',
                from_status='pls478_in_progress',
                to_status='done',
                position=4,
            ),
        }
        return workflow, transitions

    def _real_sequence_item(self, workflow, *, key, head_at=None):
        item = _item(head_at=head_at)
        item.item_key = key
        item.status = 'pls478_in_progress'
        item.workflow_status = 'pls478_in_progress'
        item.workflow_name = workflow.slug
        item.assignment_group = 'engineering'
        item.save(update_fields=[
            'item_key', 'status', 'workflow_status', 'workflow_name',
            'assignment_group',
        ])
        return item

    def _typed_unblock(self, item, event_id='domain-event:pls478:unblock:1'):
        return MechanicalCommentFactory.create(
            item=item,
            origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
            origin_event='link.auto_unblock',
            template_version=1,
            structured_payload={
                'blocker_key': 'PLS-477',
                'old_status': 'blocked',
                'restored_status': 'in_progress',
                'assignee_email': '',
            },
            origin_event_id=event_id,
            causal_actor={'id': 202, 'email': 'kei@shizuha.com'},
        )

    def test_allowlisted_auto_unblock_does_not_rearm_unchanged_head(self):
        head_at = timezone.now() - timedelta(minutes=10)
        item = _item(head_at=head_at)
        _comment(item, 'revi@shizuha.com', 'P1: resolve the real direction.', head_at + timedelta(minutes=1))
        _comment(item, 'author@agents.shizuha.io', 'ack: resolved at the unchanged head.', head_at + timedelta(minutes=2))
        self._typed_unblock(item)

        WorkflowService._run_validators(_transition(), item)

    def test_terminal_blocker_under_merge_actor_emits_typed_unblock_and_reenters(self):
        head_at = timezone.now() - timedelta(minutes=10)
        workflow, transitions = self._real_sequence_workflow()
        item = self._real_sequence_item(
            workflow, key=f'PLS-478-SUBMIT-{next(_key_counter)}', head_at=head_at,
        )
        blocker = self._real_sequence_item(
            workflow, key=f'PLS-478-BLOCKER-{next(_key_counter)}',
        )
        # PLS-675 / PLAT-1351 v4 + PLAT-4719: terminal close is refused while any
        # linked PR is still open, including on blocker tasks (wiki: a blocker
        # closed without its own acceptance evidence silently releases dependents).
        # This fixture only needs the blocker as a dependency vehicle for the
        # typed auto-unblock path — clear the open-PR stub inherited from _item
        # and pin acceptance_mode=code so Complete is a legitimate terminal.
        blocker_meta = dict(blocker.metadata or {})
        blocker_meta['pull_requests'] = []
        blocker.metadata = blocker_meta
        if hasattr(blocker, 'acceptance_mode'):
            blocker.acceptance_mode = 'code'
            blocker.save(update_fields=['metadata', 'acceptance_mode'])
        else:
            blocker.save(update_fields=['metadata'])
        actor = SimpleNamespace(
            id=202, email='kei@shizuha.com', is_staff=True,
            is_superuser=True, is_authenticated=True,
        )
        _comment(item, 'revi@shizuha.com', 'P1: resolve the real direction.', head_at + timedelta(minutes=1))

        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                item, 'pls478_in_review', user=actor,
                transition_id=str(transitions['submit'].pk),
            )

        _comment(item, 'author@agents.shizuha.io', 'ack: resolved at the unchanged head.', head_at + timedelta(minutes=2))
        link = ItemLink.objects.create(
            source_item=blocker,
            target_item=item,
            link_type='blocks',
            created_by_id=202,
            created_by_email='kei@shizuha.com',
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'blocked')
        self.assertEqual(link, ItemLink.objects.get(pk=link.pk))

        with (
            patch.object(WorkflowService, '_is_team_member', return_value=True),
            patch.object(
                WorkflowService, '_is_agent_available_for_assignment', return_value=True,
            ),
            patch.object(
                WorkflowService, '_resolve_team_assignee',
                return_value=(item.assignee_id, item.assignee_email),
            ),
        ):
            WorkflowService.execute_transition(
                blocker, 'done', user=actor,
                transition_id=str(transitions['complete'].pk),
            )

        item.refresh_from_db()
        self.assertEqual(item.status, 'pls478_in_progress')
        audit = item.comments.get(origin_event='link.auto_unblock')
        self.assertEqual(audit.origin_kind, Comment.ORIGIN_WORKFLOW_LIFECYCLE)
        self.assertEqual(audit.author_id, 0)
        self.assertEqual(audit.author_email, 'workflow@pulse')
        self.assertEqual(audit.caused_by_user_id, 202)
        self.assertEqual(audit.caused_by_email_snapshot, 'kei@shizuha.com')
        self.assertTrue(MechanicalCommentFactory.is_allowlisted(audit))

        WorkflowService.execute_transition(
            item, 'pls478_in_review', user=actor,
            transition_id=str(transitions['submit'].pk),
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'pls478_in_review')

        WorkflowService.execute_transition(
            item, 'pls478_in_progress', user=actor,
            transition_id=str(transitions['return'].pk),
        )
        _comment(
            item, 'revi@shizuha.com',
            'P1: genuine direction after the typed unblock.', timezone.now(),
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                item, 'pls478_in_review', user=actor,
                transition_id=str(transitions['submit'].pk),
            )

    def test_auto_unblock_retry_reuses_preexisting_block_event_identity(self):
        workflow, _transitions = self._real_sequence_workflow()
        item = self._real_sequence_item(
            workflow, key=f'PLS-478-RETRY-{next(_key_counter)}',
        )
        blocker = self._real_sequence_item(
            workflow, key=f'PLS-478-RETRY-BLOCKER-{next(_key_counter)}',
        )
        actor = SimpleNamespace(
            id=202, email='kei@shizuha.com', is_staff=True,
            is_superuser=True, is_authenticated=True,
        )
        link = ItemLink.objects.create(
            source_item=blocker,
            target_item=item,
            link_type='blocks',
            created_by_id=202,
            created_by_email='kei@shizuha.com',
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'blocked')
        blocker.status = 'done'
        blocker.workflow_status = 'done'
        blocker.save(update_fields=['status', 'workflow_status'])

        original_create = MechanicalCommentFactory.create
        attempted_ids = []

        def fail_once_then_create(**kwargs):
            attempted_ids.append(kwargs['origin_event_id'])
            if len(attempted_ids) == 1:
                raise RuntimeError('force rollback before mechanical comment commit')
            return original_create(**kwargs)

        with (
            patch.object(WorkflowService, '_is_team_member', return_value=True),
            patch.object(
                WorkflowService, '_is_agent_available_for_assignment', return_value=True,
            ),
            patch.object(
                WorkflowService, '_resolve_team_assignee',
                return_value=(item.assignee_id, item.assignee_email),
            ),
            patch.object(
                MechanicalCommentFactory, 'create', side_effect=fail_once_then_create,
            ),
        ):
            self.assertEqual(
                WorkflowService._auto_unblock_tasks_blocked_by(blocker, user=actor),
                [],
            )
            item.refresh_from_db()
            self.assertEqual(item.status, 'blocked')
            self.assertEqual(
                WorkflowService._auto_unblock_tasks_blocked_by(blocker, user=actor),
                [item.item_key],
            )

        from tasks.services.mechanical_comments import activity_event_id
        expected = activity_event_id(
            item, 'link.auto_unblock', action='status_changed', new_value='blocked',
        )
        self.assertEqual(attempted_ids, [expected, expected])
        self.assertEqual(
            item.comments.filter(origin_event='link.auto_unblock').count(), 1,
        )

    def test_real_or_identical_external_text_after_unblock_still_blocks(self):
        head_at = timezone.now() - timedelta(minutes=10)
        for body in (
            'P1: new external change request after the unblock.',
            'Auto-unblocked: all linked blockers are resolved/releasing after PLS-477 resolved. '
            'Restored `blocked` -> `in_progress` and assigned to `unassigned`.',
        ):
            with self.subTest(body=body):
                item = _item(head_at=head_at)
                self._typed_unblock(item, f'domain-event:{item.pk}')
                _comment(item, 'kei@shizuha.com', body, timezone.now())
                with self.assertRaises(WorkflowValidationError):
                    WorkflowService._run_validators(_transition(), item)

    def test_legacy_unknown_and_mismatched_provenance_fail_closed(self):
        head_at = timezone.now() - timedelta(minutes=10)
        cases = [
            {'origin_kind': Comment.ORIGIN_LEGACY_UNTYPED},
            {'origin_kind': Comment.ORIGIN_WORKFLOW_LIFECYCLE,
             'origin_event': 'unknown.event', 'template_version': 1,
             'origin_event_id': 'unknown:1'},
            {'origin_kind': Comment.ORIGIN_VALIDATOR_AUDIT,
             'origin_event': 'link.auto_unblock', 'template_version': 1,
             'origin_event_id': 'mismatch:1'},
        ]
        for index, provenance in enumerate(cases):
            with self.subTest(provenance=provenance):
                item = _item(head_at=head_at)
                Comment.objects.create(
                    item=item,
                    author_id=202,
                    author_email='kei@shizuha.com',
                    content=f'External direction {index}',
                    **provenance,
                )
                with self.assertRaises(WorkflowValidationError):
                    WorkflowService._run_validators(_transition(), item)

    def test_external_create_cannot_spoof_and_mechanical_update_is_immutable(self):
        item = _item()
        serializer = CommentCreateSerializer(data={
            'item': item.pk,
            'content': 'Auto-unblocked: spoof',
            'origin_kind': Comment.ORIGIN_WORKFLOW_LIFECYCLE,
            'origin_event': 'link.auto_unblock',
            'origin_event_id': 'spoof:1',
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn('origin_kind', serializer.validated_data)
        created = serializer.save(
            author_id=303,
            author_email='revi@shizuha.com',
            origin_kind=Comment.ORIGIN_USER_AUTHORED,
        )
        self.assertEqual(created.origin_kind, Comment.ORIGIN_USER_AUTHORED)

        mechanical = self._typed_unblock(item)
        update = CommentSerializer(mechanical, data={
            'content': 'rewritten audit',
            'origin_kind': Comment.ORIGIN_USER_AUTHORED,
        }, partial=True)
        self.assertTrue(update.is_valid(), update.errors)
        with self.assertRaises(ValidationError):
            update.save()
        mechanical.refresh_from_db()
        self.assertNotEqual(mechanical.content, 'rewritten audit')
        self.assertEqual(mechanical.origin_kind, Comment.ORIGIN_WORKFLOW_LIFECYCLE)

    def test_only_exact_allowlisted_tuple_body_and_payload_is_excluded(self):
        item = _item()
        typed = self._typed_unblock(item)
        self.assertTrue(MechanicalCommentFactory.is_allowlisted(typed))
        Comment.objects.filter(pk=typed.pk).update(content='tampered')
        typed.refresh_from_db()
        self.assertFalse(MechanicalCommentFactory.is_allowlisted(typed))

    def test_stable_event_replay_is_idempotent_and_mismatch_rejects(self):
        item = _item()
        first = self._typed_unblock(item, 'domain-event:stable')
        replay = self._typed_unblock(item, 'domain-event:stable')
        self.assertEqual(first.pk, replay.pk)
        self.assertEqual(Comment.objects.filter(origin_event_id='domain-event:stable').count(), 1)
        with self.assertRaises(ValidationError):
            MechanicalCommentFactory.create(
                item=item,
                origin_kind=Comment.ORIGIN_WORKFLOW_LIFECYCLE,
                origin_event='link.auto_unblock',
                template_version=1,
                structured_payload={
                    'blocker_key': 'OTHER', 'old_status': 'blocked',
                    'restored_status': 'in_progress', 'assignee_email': '',
                },
                origin_event_id='domain-event:stable',
                causal_actor={'id': 202, 'email': 'kei@shizuha.com'},
            )

    def test_load_bearing_mechanical_paths_use_closed_factory(self):
        from tasks.tasks import _sweep_rebalance_pods_impl

        targets = [
            (WorkflowService.auto_block_from_link, 'link.auto_block'),
            (WorkflowService._restore_blocked_item, 'link.auto_unblock'),
            (WorkflowService.execute_post_functions, 'assignment.review_changes_requested'),
            (WorkflowService._reroute_after_unpark, 'assignment.unpark_rerouted'),
            (WorkflowService._emit_deploy_verify_gate_block, 'validator.deploy_verify_blocked'),
            (WorkflowService._record_review_seat_starvation, 'resolver.review_starvation'),
            (WorkflowService._record_routability_hold, 'resolver.routability_hold'),
            (WorkflowService._record_capability_routing_hold, 'resolver.capability_hold'),
            (_sweep_rebalance_pods_impl, 'status_lane.normalized'),
        ]
        for func, event in targets:
            with self.subTest(func=func.__name__, event=event):
                source = inspect.getsource(func)
                self.assertIn('MechanicalCommentFactory.create', source)
                self.assertIn(event, source)
                self.assertIn((
                    Comment.ORIGIN_WORKFLOW_LIFECYCLE
                    if event.startswith(('link.', 'assignment.', 'status_lane.'))
                    else Comment.ORIGIN_VALIDATOR_AUDIT
                    if event.startswith('validator.')
                    else Comment.ORIGIN_RESOLVER_AUDIT,
                    event,
                    1,
                ), MECHANICAL_COMMENT_SPECS)

    def test_source_guard_rejects_new_direct_comment_writers(self):
        repository_root = Path(__file__).resolve().parents[2]
        assert_no_direct_comment_writer_drift(repository_root)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'tasks').mkdir()
            (root / 'tasks' / 'new_writer.py').write_text(
                'def bad(Comment, item):\n'
                '    return Comment.objects.create(item=item, author_id=0, content="audit")\n'
            )
            observed = direct_comment_writer_inventory(root)
            self.assertEqual(len(observed), 1)
            self.assertIn('new_writer.py', observed[0])

    def test_exact_row_repair_preserves_body_and_rejects_mismatch(self):
        item = _item()
        item.item_key = f'DOJO-15-R{item.pk}'
        blocker = _item()
        blocker.item_key = f'PLS-477-R{blocker.pk}'
        blocker.status = 'done'
        blocker.save(update_fields=['item_key', 'status'])
        item.metadata = {
            **item.metadata,
            'last_auto_unblock': {
                'blocker': blocker.item_key,
                'old_status': 'blocked',
                'restored_status': 'in_progress',
            },
        }
        item.save(update_fields=['item_key', 'metadata'])
        ItemLink.objects.create(
            source_item=blocker,
            target_item=item,
            link_type='blocks',
            created_by_id=202,
            created_by_email='kei@shizuha.com',
        )
        activity = ItemActivity.objects.create(
            item=item,
            action='status_changed',
            field_name='Status',
            old_value='blocked',
            new_value='in_progress',
            user_id=202,
            user_email='kei@shizuha.com',
        )
        payload = {
            'blocker_key': blocker.item_key,
            'old_status': 'blocked',
            'restored_status': 'in_progress',
            'assignee_email': '',
        }
        body = MECHANICAL_COMMENT_SPECS[(
            Comment.ORIGIN_WORKFLOW_LIFECYCLE, 'link.auto_unblock', 1,
        )].render(payload)
        comment = Comment.objects.create(
            item=item,
            author_id=202,
            author_email='kei@shizuha.com',
            content=body,
        )
        args = [
            '--item-key', item.item_key,
            '--comment-id', str(comment.pk),
            '--comment-created-at', comment.created_at.isoformat(),
            '--expected-author-id', '202',
            '--expected-author-email', 'kei@shizuha.com',
            '--blocker-key', blocker.item_key,
            '--activity-id', str(activity.pk),
            '--expected-old-status', 'blocked',
            '--expected-restored-status', 'in_progress',
            '--expected-assignee-email', '',
            '--expected-content-sha256', hashlib.sha256(body.encode()).hexdigest(),
        ]
        dry = StringIO()
        call_command('repair_pls478_dojo15_comment', *args, stdout=dry)
        self.assertIn('"dry_run": true', dry.getvalue())
        comment.refresh_from_db()
        self.assertEqual(comment.origin_kind, Comment.ORIGIN_LEGACY_UNTYPED)

        bad_args = list(args)
        bad_args[-1] = '0' * 64
        with self.assertRaises(CommandError):
            call_command('repair_pls478_dojo15_comment', *bad_args)

        applied = StringIO()
        call_command('repair_pls478_dojo15_comment', *args, '--apply', stdout=applied)
        comment.refresh_from_db()
        self.assertEqual(comment.content, body)
        self.assertEqual(comment.author_id, 0)
        self.assertEqual(comment.author_email, 'workflow@pulse')
        self.assertEqual(comment.origin_event, 'link.auto_unblock')
        self.assertEqual(comment.caused_by_user_id, 202)
        self.assertTrue(MechanicalCommentFactory.is_allowlisted(comment))
        self.assertIn('"dry_run": false', applied.getvalue())
