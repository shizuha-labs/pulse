"""PLS-188 / PLS-411: merge lanes must preserve ownership and reviewer SoD.

`awaiting_merge` is Merge-lane work. Unblock restore and auto-failover used to
preserve stale pre-block/review assignment_group values, so merge-ready work could
land on Review/Reika instead of the merge roster.
"""
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks import views
from .base import create_blocked_item, satisfy_blocked_invariant


class StatusLaneRoutingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.sora = User.objects.create_user(username='sora', email='sora@shizuha.com', password='x')
        self.merger = User.objects.create_user(username='merger', email='merger@shizuha.com', password='x')
        self.reika = User.objects.create_user(username='reika', email='reika@shizuha.com', password='x')
        self.aoi = User.objects.create_user(username='aoi', email='aoi@shizuha.com', password='x')
        self.kei = User.objects.create_user(username='kei', email='kei@shizuha.com', password='x')
        self.ryo = User.objects.create_user(username='ryo', email='ryo@shizuha.com', password='x')
        self.project = Project.objects.create(name='PLS188', key='P188', org_id=1)
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='merge',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[
                    'sora@shizuha.com',
                    'merger@shizuha.com',
                    'aoi@shizuha.com',
                    'kei@shizuha.com',
                ]],
                'is_active': True,
            },
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='review',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['reika@shizuha.com']],
                'is_active': True,
            },
        )

    def _user_by_email(self, email):
        by_email = {
            'sora@shizuha.com': self.sora,
            'merger@shizuha.com': self.merger,
            'reika@shizuha.com': self.reika,
            'aoi@shizuha.com': self.aoi,
            'kei@shizuha.com': self.kei,
            'ryo@shizuha.com': self.ryo,
        }
        user = by_email.get((email or '').lower())
        return {'id': user.id, 'email': user.email} if user else None

    def _patch_routing(self):
        return mock.patch.multiple(
            WorkflowService,
            _pod_roster=mock.Mock(return_value={
                'merge': ['sora@shizuha.com', 'merger@shizuha.com'],
                'review': ['reika@shizuha.com'],
            }),
            _is_agent_available_for_assignment=mock.Mock(
                side_effect=lambda email: (email or '').lower() in {
                    'merger@shizuha.com',
                    'reika@shizuha.com',
                },
            ),
            _linked_pr_current_head_reviewers=mock.Mock(return_value=(set(), [], 1)),
        )

    def test_unblock_restore_uses_status_lane_not_stale_review_group(self):
        item = create_blocked_item(
            title='merge task blocked with stale review group',
            project=self.project,
            mode='task',
            # a workflow-less task attaches 'simple', which lacks 'awaiting_merge'
            # so the restored status would coerce to 'todo'. This name is not a
            # seeded workflow, so save() applies no status coercion (mirrors a real
            # awaiting_merge task, whose workflow does contain the status).
            workflow_name='autonomous-dev',
            status='blocked',
            workflow_status='blocked',
            assignment_group='review',
            assignee_id=self.reika.id,
            assignee_email='reika@shizuha.com',
            metadata={
                'pre_block_state': {
                    'status': 'awaiting_merge',
                    'workflow_status': 'awaiting_merge',
                    'assignment_group': 'review',
                    'assignee_id': self.sora.id,
                    'assignee_email': 'sora@shizuha.com',
                    'had_active_blocker': True,
                },
            },
        )

        with self._patch_routing(), mock.patch(
            'tasks.user_service.user_service.get_user_by_email', side_effect=self._user_by_email,
        ), mock.patch.object(
            WorkflowService, '_workflow_status_slugs', return_value=set(),
        ), mock.patch.object(WorkflowService, '_active_blockers_for', return_value=[]):
            restored = WorkflowService._restore_blocked_item(item)

        self.assertTrue(restored)
        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')
        self.assertEqual(item.assignment_group, 'merge')
        self.assertEqual(item.assignee_email, 'merger@shizuha.com')
        self.assertNotEqual(item.assignee_email, 'reika@shizuha.com')

    def test_explicit_aoi_merge_handoff_survives_roster_normalizer(self):
        """KOT-27 shape: a direct SoD handoff stays on Aoi even when the
        current merge roster would otherwise normalize it back to Kei."""
        item = Item.objects.create(
            title='approved PR handed to distinct merger',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            assignee_id=self.aoi.id,
            assignee_email='aoi@shizuha.com',
            metadata={'pull_requests': [{'url': 'https://github.com/shizuha-labs/kotlin/pull/48'}]},
        )
        views._persist_direct_assignment_pin(
            item,
            'aoi@shizuha.com',
            'Distinct merger required after kai2574 review',
            actor_id=self.kei.id,
            actor_email='kei@shizuha.com',
        )
        item.refresh_from_db()
        fields = []

        with mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_linked_pr_current_head_reviewers',
            return_value=({'kai2574'}, [], 1),
        ) as review_lookup:
            lane, changed = WorkflowService._enforce_status_lane_owner(
                item, item.status, fields,
            )

        self.assertEqual(lane, 'merge')
        self.assertFalse(changed)
        self.assertEqual(fields, [])
        self.assertEqual(item.assignee_email, 'aoi@shizuha.com')
        review_lookup.assert_called_once_with(item)

    def test_merge_handoff_survives_block_state_capture_and_restore(self):
        item = Item.objects.create(
            title='merge handoff temporarily blocked',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            assignee_id=self.aoi.id,
            assignee_email='aoi@shizuha.com',
        )
        views._persist_direct_assignment_pin(
            item,
            'aoi@shizuha.com',
            'Distinct merger required after review',
            actor_id=self.kei.id,
            actor_email='kei@shizuha.com',
        )
        item.refresh_from_db()
        handoff = item.metadata['explicit_merge_handoff']

        WorkflowService._capture_pre_block_state(
            item=item,
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignee_id=self.aoi.id,
            assignee_email='aoi@shizuha.com',
            assignment_group='merge',
            percent_complete=90,
            explicit_merge_handoff=handoff,
        )
        self.assertEqual(
            item.metadata['pre_block_state']['explicit_merge_handoff'],
            handoff,
        )

        item.status = 'blocked'
        with mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=False,
        ):
            uid, email = WorkflowService._resolve_unblock_assignee(
                item,
                item.metadata['pre_block_state'],
            )

        self.assertEqual((uid, email), (self.aoi.id, 'aoi@shizuha.com'))

    def test_reviewer_identity_is_hard_excluded_and_task_group_queues(self):
        """A2 exact regression: Kei maps to the current-head reviewer identity,
        so the resolver may not self-fallback to Kei when no distinct seat exists."""
        item = Item.objects.create(
            title='KOT-27 exact reviewer conflict',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            assignee_id=self.aoi.id,
            assignee_email='aoi@shizuha.com',
            metadata={'pull_requests': [{'url': 'https://github.com/shizuha-labs/kotlin/pull/48'}]},
        )
        fields = []

        def resolve(team, **kwargs):
            self.assertEqual(team, 'merge')
            self.assertEqual(kwargs['hard_exclude_emails'], {'kei@shizuha.com'})
            return None, None

        with mock.patch.object(
            WorkflowService,
            '_pod_roster',
            return_value={'merge': ['kei@shizuha.com']},
        ), mock.patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=False,
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_linked_pr_current_head_reviewers',
            return_value=({'kai2574'}, [], 1),
        ), mock.patch.object(
            WorkflowService,
            '_agent_github_identity',
            side_effect=lambda email: {'kei@shizuha.com': 'kai2574'}.get((email or '').lower()),
        ), mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=resolve,
        ):
            lane, changed = WorkflowService._enforce_status_lane_owner(
                item, item.status, fields,
            )

        self.assertEqual(lane, 'merge')
        self.assertTrue(changed)
        self.assertIsNone(item.assignee_id)
        self.assertEqual(item.assignee_email, '')
        self.assertEqual(item.metadata['merge_sod_hold']['reason'], 'no_distinct_available_merger')
        self.assertEqual(item.metadata['merge_sod_hold']['current_head_reviewers'], ['kai2574'])
        self.assertEqual(item.metadata['merge_sod_hold']['excluded_assignees'], ['kei@shizuha.com'])

    def test_distinct_github_identity_candidate_can_be_assigned(self):
        item = Item.objects.create(
            title='approved PR with distinct merger available',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            assignee_id=self.kei.id,
            assignee_email='kei@shizuha.com',
            metadata={'pull_requests': [{'url': 'https://github.com/org/repo/pull/7'}]},
        )
        fields = []
        identities = {
            'kei@shizuha.com': 'kai2574',
            'merger@shizuha.com': 'distinct-merger',
        }

        with mock.patch.object(
            WorkflowService,
            '_pod_roster',
            return_value={'merge': ['kei@shizuha.com', 'merger@shizuha.com']},
        ), mock.patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_linked_pr_current_head_reviewers',
            return_value=({'kai2574'}, [], 1),
        ), mock.patch.object(
            WorkflowService,
            '_agent_github_identity',
            side_effect=lambda email: identities.get((email or '').lower()),
        ), mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.merger.id, 'merger@shizuha.com'),
        ):
            lane, changed = WorkflowService._enforce_status_lane_owner(
                item, item.status, fields,
            )

        self.assertEqual(lane, 'merge')
        self.assertTrue(changed)
        self.assertEqual(item.assignee_email, 'merger@shizuha.com')
        self.assertNotIn('merge_sod_hold', item.metadata)

    def test_shared_seat_verdict_excludes_every_email_on_reviewer_identity(self):
        """PLS-454: email aliases/seats do not weaken GitHub-identity SoD."""
        item = Item.objects.create(
            title='held merge task with reviewer identity alias',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            metadata={
                'merge_sod_hold': {
                    'reason': 'no_distinct_available_merger',
                    'current_head_reviewers': ['kai2574'],
                    'excluded_assignees': ['kei@shizuha.com'],
                    'unknown_identity_assignees': [],
                },
                'pull_requests': [
                    {'url': 'https://github.com/shizuha-labs/kotlin/pull/66'},
                ],
            },
        )

        with mock.patch.object(
            WorkflowService,
            '_linked_pr_current_head_reviewers',
            return_value=({'kai2574'}, [], 1),
        ), mock.patch.object(
            WorkflowService,
            '_agent_github_identity',
            side_effect=lambda email: {
                'kei@shizuha.com': 'kai2574',
                'ryo@shizuha.com': 'kai2574',
                'merger@shizuha.com': 'distinct-merger',
            }.get((email or '').lower()),
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_assignment', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService,
            'assignable_teams',
            return_value={'merge': ['kei@shizuha.com', 'ryo@shizuha.com', 'merger@shizuha.com']},
        ):
            kei = WorkflowService._seat_routability_verdict(
                item, 'merge', 'kei@shizuha.com', user_id=self.kei.id,
            )
            ryo_alias = WorkflowService._seat_routability_verdict(
                item, 'merge', 'ryo@shizuha.com', user_id=999,
            )
            distinct = WorkflowService._seat_routability_verdict(
                item, 'merge', 'merger@shizuha.com', user_id=self.merger.id,
            )

        self.assertIn('merge_sod_reviewer_identity', kei.reasons)
        self.assertIn('merge_sod_reviewer_identity', ryo_alias.reasons)
        self.assertNotIn('merge_sod_reviewer_identity', distinct.reasons)

    def test_shared_seat_verdict_fails_closed_when_candidate_identity_unknown(self):
        item = Item.objects.create(
            title='held merge task with unknown candidate identity',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            metadata={
                'merge_sod_hold': {
                    'reason': 'no_distinct_available_merger',
                    'current_head_reviewers': ['kai2574'],
                },
            },
        )

        with mock.patch.object(
            WorkflowService,
            '_linked_pr_current_head_reviewers',
            return_value=(set(), [], 1),
        ), mock.patch.object(
            WorkflowService, '_agent_github_identity', return_value=None,
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_assignment', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService,
            'assignable_teams',
            return_value={'merge': ['merger@shizuha.com']},
        ):
            verdict = WorkflowService._seat_routability_verdict(
                item, 'merge', 'merger@shizuha.com', user_id=self.merger.id,
            )

        self.assertIn('merge_sod_identity_unavailable', verdict.reasons)
        with mock.patch.object(WorkflowService, '_routability_enforced', return_value=False):
            self.assertFalse(WorkflowService._seat_assignment_allowed(verdict))

    def test_pls497_aoi_distinct_forgejo_identity_routes_despite_kai2574_reviewer(self):
        """PLS-497: reviewer kai2574 must not exclude Aoi when Aoi's Origin login is ``aoi``."""
        item = Item.objects.create(
            title='PLAT-4070 merge starvation shape',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            assignee_id=None,
            assignee_email='',
            metadata={
                'merge_sod_hold': {
                    'reason': 'no_distinct_available_merger',
                    'current_head_reviewers': ['kai2574'],
                    'excluded_assignees': ['aoi@shizuha.com'],
                },
                'pull_requests': [
                    {'url': 'https://github.com/shizuha-labs/shizuha/pull/83'},
                ],
            },
        )
        fields = []
        identities = {
            'aoi@shizuha.com': 'aoi',
            'kei@shizuha.com': 'kai2574',
        }

        with mock.patch.object(
            WorkflowService,
            '_pod_roster',
            return_value={'merge': ['aoi@shizuha.com', 'kei@shizuha.com']},
        ), mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_assignment', return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_linked_pr_current_head_reviewers',
            return_value=({'kai2574'}, [], 1),
        ), mock.patch.object(
            WorkflowService,
            '_agent_github_identity',
            side_effect=lambda email: identities.get((email or '').lower()),
        ), mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.aoi.id, 'aoi@shizuha.com'),
        ):
            # Direct SoD reasons while the durable hold is still present.
            aoi_reasons = WorkflowService._merge_sod_candidate_reasons(
                item, 'aoi@shizuha.com',
            )
            kei_reasons = WorkflowService._merge_sod_candidate_reasons(
                item, 'kei@shizuha.com',
            )
            lane, changed = WorkflowService._enforce_status_lane_owner(
                item, item.status, fields,
            )

        self.assertEqual(lane, 'merge')
        self.assertTrue(changed)
        self.assertEqual(item.assignee_email, 'aoi@shizuha.com')
        self.assertNotEqual(item.assignee_email, 'kei@shizuha.com')
        self.assertNotIn('merge_sod_hold', item.metadata)
        self.assertEqual(aoi_reasons, ())
        self.assertIn('merge_sod_reviewer_identity', kei_reasons)

    def test_pls497_same_identity_still_denied(self):
        """Hard SoD retained: candidate whose SCM login matches reviewer is excluded."""
        item = Item.objects.create(
            title='same-identity merger denied',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            metadata={
                'merge_sod_hold': {
                    'reason': 'no_distinct_available_merger',
                    'current_head_reviewers': ['aoi'],
                },
                'pull_requests': [
                    {'url': 'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/9'},
                ],
            },
        )
        with mock.patch.object(
            WorkflowService,
            '_linked_origin_pr_authors',
            return_value=({'other-author'}, [], 1),
        ), mock.patch.object(
            WorkflowService,
            '_linked_pr_current_head_reviewers',
            return_value=({'aoi'}, [], 1),
        ), mock.patch.object(
            WorkflowService,
            '_agent_github_identity',
            side_effect=lambda email: {
                'aoi@shizuha.com': 'aoi',
                'merger@shizuha.com': 'merger',
            }.get((email or '').lower()),
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_assignment', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService,
            'assignable_teams',
            return_value={'merge': ['aoi@shizuha.com', 'merger@shizuha.com']},
        ):
            same = WorkflowService._seat_routability_verdict(
                item, 'merge', 'aoi@shizuha.com', user_id=self.aoi.id,
            )
            distinct = WorkflowService._seat_routability_verdict(
                item, 'merge', 'merger@shizuha.com', user_id=self.merger.id,
            )
        self.assertIn('merge_sod_reviewer_identity', same.reasons)
        self.assertNotIn('merge_sod_reviewer_identity', distinct.reasons)
        self.assertTrue(WorkflowService._seat_assignment_allowed(distinct))
        self.assertFalse(WorkflowService._seat_assignment_allowed(same))

    def test_pls564_author_is_excluded_then_distinct_live_merger_routes(self):
        """Production order: author live + distinct seat down, then Ryo live."""
        item = Item.objects.create(
            title='ORIG-120 author fallback shape',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            assignee_id=self.aoi.id,
            assignee_email='aoi@shizuha.com',
            metadata={
                'pull_requests': [{
                    'url': (
                        'https://origin.shizuha.com/shizuha-labs/'
                        'deploy/pulls/58'
                    ),
                    'status': 'open',
                    'head_sha': 'current-head',
                }],
            },
        )
        fields = []
        ryo_live = {'value': False}

        def resolve(team, **kwargs):
            self.assertEqual(team, 'merge')
            self.assertIn('aoi@shizuha.com', kwargs['hard_exclude_emails'])
            if ryo_live['value']:
                return self.ryo.id, self.ryo.email
            return None, None

        def available(email):
            email = (email or '').lower()
            return email == 'aoi@shizuha.com' or (
                email == 'ryo@shizuha.com' and ryo_live['value']
            )

        with mock.patch.multiple(
            WorkflowService,
            _pod_roster=mock.Mock(return_value={
                'merge': ['aoi@shizuha.com', 'kei@shizuha.com', 'ryo@shizuha.com'],
            }),
            _is_team_member=mock.Mock(return_value=True),
            _is_agent_available_for_assignment=mock.Mock(side_effect=available),
            _linked_origin_pr_authors=mock.Mock(return_value=({'aoi2574'}, [], 1)),
            _linked_pr_current_head_reviewers=mock.Mock(
                return_value=({'revi'}, [], 1),
            ),
            _resolve_team_assignee=mock.Mock(side_effect=resolve),
        ):
            lane, changed = WorkflowService._enforce_status_lane_owner(
                item, item.status, fields,
            )
            self.assertEqual(lane, 'merge')
            self.assertTrue(changed)
            self.assertIsNone(item.assignee_id)
            self.assertEqual(item.assignee_email, '')
            self.assertEqual(
                item.metadata['merge_sod_hold']['current_pr_authors'],
                ['aoi2574'],
            )
            self.assertIn(
                'aoi@shizuha.com',
                item.metadata['merge_sod_hold']['excluded_assignees'],
            )

            ryo_live['value'] = True
            lane, changed = WorkflowService._enforce_status_lane_owner(
                item, item.status, fields,
            )

        self.assertEqual(lane, 'merge')
        self.assertTrue(changed)
        self.assertEqual(item.assignee_id, self.ryo.id)
        self.assertEqual(item.assignee_email, 'ryo@shizuha.com')
        self.assertNotIn('merge_sod_hold', item.metadata)

    def test_pls564_shared_seat_verdict_hard_denies_pr_author(self):
        item = Item.objects.create(
            title='author cannot occupy merger seat',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            metadata={
                'pull_requests': [{
                    'url': (
                        'https://origin.shizuha.com/shizuha-labs/'
                        'deploy/pulls/58'
                    ),
                    'status': 'open',
                    'head_sha': 'current-head',
                }],
            },
        )
        with mock.patch.multiple(
            WorkflowService,
            _linked_origin_pr_authors=mock.Mock(return_value=({'aoi2574'}, [], 1)),
            _linked_pr_current_head_reviewers=mock.Mock(
                return_value=({'revi'}, [], 1),
            ),
            _is_agent_available_for_assignment=mock.Mock(return_value=True),
            _member_can_execute_from_status=mock.Mock(return_value=True),
            assignable_teams=mock.Mock(
                return_value={'merge': ['aoi@shizuha.com', 'ryo@shizuha.com']},
            ),
            _is_team_member=mock.Mock(return_value=True),
        ):
            author = WorkflowService._seat_routability_verdict(
                item, 'merge', 'aoi@shizuha.com', user_id=self.aoi.id,
            )
            distinct = WorkflowService._seat_routability_verdict(
                item, 'merge', 'ryo@shizuha.com', user_id=self.ryo.id,
            )

        self.assertIn('merge_sod_author_identity', author.reasons)
        self.assertFalse(WorkflowService._seat_assignment_allowed(author))
        self.assertNotIn('merge_sod_author_identity', distinct.reasons)
        self.assertTrue(WorkflowService._seat_assignment_allowed(distinct))

    def test_pls564_signed_distinct_handoff_survives_author_filter(self):
        roster = TeamRoster.objects.get(organization_id=1, team_name='merge')
        roster.tiers = [list(roster.tiers[0]) + ['ryo@shizuha.com']]
        roster.save(update_fields=['tiers'])
        item = Item.objects.create(
            title='signed distinct merger handoff',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            assignee_id=self.ryo.id,
            assignee_email='ryo@shizuha.com',
            metadata={
                'pull_requests': [{
                    'url': (
                        'https://origin.shizuha.com/shizuha-labs/'
                        'deploy/pulls/58'
                    ),
                    'status': 'open',
                    'head_sha': 'current-head',
                }],
            },
        )
        views._persist_direct_assignment_pin(
            item,
            'ryo@shizuha.com',
            'Independent merger handoff for PLS-564',
            actor_id=self.kei.id,
            actor_email='kei@shizuha.com',
        )
        item.refresh_from_db()
        resolver = mock.Mock()

        with mock.patch.multiple(
            WorkflowService,
            _is_agent_available_for_assignment=mock.Mock(return_value=True),
            _linked_origin_pr_authors=mock.Mock(return_value=({'aoi'}, [], 1)),
            _linked_pr_current_head_reviewers=mock.Mock(
                return_value=({'revi'}, [], 1),
            ),
            _is_team_member=mock.Mock(return_value=False),
            _resolve_team_assignee=resolver,
        ):
            lane, _changed = WorkflowService._enforce_status_lane_owner(
                item, item.status, [],
            )

        self.assertEqual(lane, 'merge')
        self.assertEqual(item.assignee_email, 'ryo@shizuha.com')
        self.assertEqual(
            item.metadata['explicit_merge_handoff']['assignee_email'],
            'ryo@shizuha.com',
        )
        resolver.assert_not_called()

    def test_pls564_numeric_author_alias_denies_signed_owner_and_fallbacks(self):
        """Legacy Forgejo author aliases cannot survive any merger path."""
        roster = TeamRoster.objects.get(organization_id=1, team_name='merge')
        roster.tiers = [list(roster.tiers[0]) + ['ryo@shizuha.com']]
        roster.save(update_fields=['tiers'])
        item = Item.objects.create(
            title='numeric-suffixed author handoff denial',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            assignee_id=self.aoi.id,
            assignee_email='aoi@shizuha.com',
            metadata={
                'pull_requests': [{
                    'url': (
                        'https://origin.shizuha.com/shizuha-labs/'
                        'deploy/pulls/58'
                    ),
                    'status': 'open',
                    'head_sha': 'current-head',
                }],
            },
        )
        views._persist_direct_assignment_pin(
            item,
            'aoi@shizuha.com',
            'Signed handoff must not bypass numeric author alias',
            actor_id=self.kei.id,
            actor_email='kei@shizuha.com',
        )
        item.refresh_from_db()

        def resolve(team, **kwargs):
            self.assertEqual(team, 'merge')
            self.assertIn('aoi@shizuha.com', kwargs['hard_exclude_emails'])
            return self.ryo.id, self.ryo.email

        with mock.patch.multiple(
            WorkflowService,
            _pod_roster=mock.Mock(return_value={
                'merge': ['aoi@shizuha.com', 'ryo@shizuha.com'],
            }),
            _is_agent_available_for_assignment=mock.Mock(return_value=True),
            _linked_origin_pr_authors=mock.Mock(
                return_value=({'aoi2574'}, [], 1),
            ),
            _linked_pr_current_head_reviewers=mock.Mock(
                return_value=({'revi'}, [], 1),
            ),
            _is_team_member=mock.Mock(return_value=True),
            _member_can_execute_from_status=mock.Mock(return_value=True),
            assignable_teams=mock.Mock(return_value={
                'merge': ['aoi@shizuha.com', 'ryo@shizuha.com'],
            }),
            _resolve_team_assignee=mock.Mock(side_effect=resolve),
        ):
            alias_verdict = WorkflowService._seat_routability_verdict(
                item, 'merge', 'aoi@shizuha.com', user_id=self.aoi.id,
            )
            lane, changed = WorkflowService._enforce_status_lane_owner(
                item, item.status, [],
            )

        self.assertIn('merge_sod_author_identity', alias_verdict.reasons)
        self.assertFalse(WorkflowService._seat_assignment_allowed(alias_verdict))
        self.assertEqual(lane, 'merge')
        self.assertTrue(changed)
        self.assertEqual(item.assignee_email, 'ryo@shizuha.com')
        self.assertNotIn('explicit_merge_handoff', item.metadata)

    def test_pls564_noncanonical_pr_group_queues_without_request(self):
        item = Item.objects.create(
            title='hostile linked PR evidence',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            assignee_id=self.aoi.id,
            assignee_email='aoi@shizuha.com',
            metadata={
                'pull_requests': [{
                    'url': (
                        'https://origin.shizuha.com.attacker.invalid/'
                        'shizuha-labs/deploy/pulls/58'
                    ),
                    'status': 'open',
                    'head_sha': 'current-head',
                }],
            },
        )

        with mock.patch('requests.get') as request_get, mock.patch.multiple(
            WorkflowService,
            _pod_roster=mock.Mock(return_value={
                'merge': ['aoi@shizuha.com', 'ryo@shizuha.com'],
            }),
            _is_team_member=mock.Mock(return_value=True),
            _is_agent_available_for_assignment=mock.Mock(return_value=True),
            _resolve_team_assignee=mock.Mock(
                return_value=(self.aoi.id, self.aoi.email),
            ),
        ):
            lane, changed = WorkflowService._enforce_status_lane_owner(
                item, item.status, [],
            )

        self.assertEqual(lane, 'merge')
        self.assertTrue(changed)
        self.assertIsNone(item.assignee_id)
        self.assertEqual(item.assignee_email, '')
        self.assertEqual(
            item.metadata['merge_sod_hold']['reason'],
            'sod_evidence_unavailable',
        )
        request_get.assert_not_called()

    def test_pls497_scm_identity_local_part_default(self):
        self.assertEqual(WorkflowService._agent_scm_identity('aoi@shizuha.com'), 'aoi')
        self.assertEqual(WorkflowService._agent_scm_identity('kei@shizuha.com'), 'kei')
        self.assertEqual(WorkflowService._agent_github_identity('aoi@shizuha.com'), 'aoi')

    def test_merge_hold_then_rebalance_tick_stays_group_queued(self):
        """KOT-102 exact ordering: normalizer hold, then generic PLAT-236/867 pull."""
        from tasks.tasks import _sweep_rebalance_pods_impl

        item = Item.objects.create(
            title='KOT-102 held merge ordering regression',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            priority='urgent',
            metadata={
                'pull_requests': [
                    {'url': 'https://github.com/shizuha-labs/deploy/pull/453'},
                    {'url': 'https://github.com/shizuha-labs/kotlin/pull/66'},
                ],
            },
        )

        with mock.patch.multiple(
            WorkflowService,
            # PLAT-4213 made the item-scoped organization roster authoritative;
            # patch that SoT rather than the legacy compatibility roster.
            _team_roster_entry_for_item=mock.Mock(
                return_value=([['kei@shizuha.com']], 'shizuha'),
            ),
            _is_agent_available_for_assignment=mock.Mock(return_value=True),
            _disabled_agent_emails=mock.Mock(return_value=set()),
            _linked_pr_current_head_reviewers=mock.Mock(
                return_value=({'kai2574'}, [], 2),
            ),
            _agent_github_identity=mock.Mock(return_value='kai2574'),
            _member_can_execute_from_status=mock.Mock(return_value=True),
        ), mock.patch(
            'tasks.user_service.user_service.get_user_by_email',
            side_effect=self._user_by_email,
        ), mock.patch.dict(
            os.environ,
            {
                'PULSE_HEALTH_EXPORTER_URL': '',
                'PULSE_API_HEALTH_URL': 'disabled',
                'PULSE_ROUTABILITY_ENFORCE': 'shadow',
            },
            clear=False,
        ):
            _sweep_rebalance_pods_impl(max_skew=0)

        item.refresh_from_db()
        self.assertIsNone(item.assignee_id)
        self.assertIn(item.assignee_email, ('', None))
        self.assertEqual(item.assignment_group, 'merge')
        self.assertEqual(
            item.metadata['merge_sod_hold']['current_head_reviewers'],
            ['kai2574'],
        )
        self.assertFalse(
            item.comments.filter(content__contains='rebalance sweep found headroom').exists()
        )

    def test_direct_assignment_persists_explicit_merge_handoff(self):
        item = Item.objects.create(
            title='manual merger handoff',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            assignee_id=self.aoi.id,
            assignee_email='aoi@shizuha.com',
        )

        views._persist_direct_assignment_pin(
            item,
            'aoi@shizuha.com',
            'Distinct merger required for reviewer separation',
            actor_id=self.kei.id,
            actor_email='kei@shizuha.com',
        )
        item.refresh_from_db()

        self.assertEqual(
            item.metadata['explicit_merge_handoff']['assignee_email'],
            'aoi@shizuha.com',
        )
        self.assertIn('reviewer separation', item.metadata['explicit_merge_handoff']['reason'])
        self.assertEqual(
            item.metadata['explicit_merge_handoff']['assigned_by_email'],
            'kei@shizuha.com',
        )

    def test_client_writable_handoff_metadata_without_audit_row_is_rejected(self):
        item = Item.objects.create(
            title='forged merger handoff marker',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            assignee_id=self.kei.id,
            assignee_email='kei@shizuha.com',
            metadata={
                'explicit_merge_handoff': {
                    'assignee_email': 'kei@shizuha.com',
                    'assigned_by_email': 'kei@shizuha.com',
                    'reason': 'forged through item metadata PATCH',
                },
            },
        )

        self.assertEqual(WorkflowService._explicit_merge_handoff_email(item), '')

    def test_github_lookup_uses_only_current_head_reviews(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        cache.clear()
        responses = [
            Response({'head': {'sha': 'current-head'}}),
            Response([
                {
                    'commit_id': 'stale-head',
                    'state': 'APPROVED',
                    'user': {'login': 'stale-reviewer'},
                },
                {
                    'commit_id': 'current-head',
                    'state': 'APPROVED',
                    'user': {'login': 'Kai2574'},
                },
                {
                    'commit_id': 'current-head',
                    'state': 'DISMISSED',
                    'user': {'login': 'dismissed-reviewer'},
                },
            ]),
        ]
        with mock.patch.dict(os.environ, {'GITHUB_TOKEN': 'test-token'}, clear=False), mock.patch(
            'requests.get', side_effect=responses,
        ):
            reviewers, error = WorkflowService._github_current_head_reviewers({
                'url': 'https://github.com/example/current-head-test/pull/411',
            })

        self.assertIsNone(error)
        self.assertEqual(reviewers, {'kai2574'})

    def test_rebalance_normalizes_awaiting_merge_before_review_failover(self):
        from tasks.tasks import sweep_rebalance_pods

        item = Item.objects.create(
            title='merge task stranded in review lane',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',  # unseeded → no status coercion
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='review',
            assignee_id=self.reika.id,
            assignee_email='reika@shizuha.com',
            metadata={'pull_requests': [{'url': 'https://github.com/org/repo/pull/1'}]},
        )

        with self._patch_routing(), mock.patch(
            'tasks.user_service.user_service.get_user_by_email', side_effect=self._user_by_email,
        ), mock.patch.object(
            WorkflowService, '_disabled_agent_emails', return_value={'sora@shizuha.com'},
        ), mock.patch.dict(
            'os.environ',
            {'PULSE_HEALTH_EXPORTER_URL': '', 'PULSE_API_HEALTH_URL': 'disabled'},
            clear=False,
        ):
            sweep_rebalance_pods.apply(args=[], kwargs={'max_skew': 0})

        item.refresh_from_db()
        self.assertEqual(item.assignment_group, 'merge')
        self.assertEqual(item.assignee_email, 'merger@shizuha.com')
        self.assertNotEqual(item.assignee_email, 'reika@shizuha.com')

    def test_rebalance_preserves_blocked_explicit_handoff_after_live_recheck(self):
        """The live PLS-411 shape: a blocked awaiting_merge item is in a stale
        down snapshot, but Aoi is live again before the drain writes.  The signed
        handoff must survive and may not fail over to the reviewer/another seat.
        """
        from tasks.tasks import sweep_rebalance_pods

        item = create_blocked_item(
            title='blocked merge handoff in stale down snapshot',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='blocked',
            workflow_status='blocked',
            assignment_group='merge',
            assignee_id=self.aoi.id,
            assignee_email='aoi@shizuha.com',
            metadata={
                'pre_block_state': {
                    'status': 'awaiting_merge',
                    'workflow_status': 'Awaiting Merge',
                    'assignment_group': 'merge',
                    'assignee_id': self.aoi.id,
                    'assignee_email': 'aoi@shizuha.com',
                    'had_active_blocker': True,
                },
                'pull_requests': [
                    {'url': 'https://github.com/shizuha-labs/home/pull/47'},
                ],
            },
        )
        satisfy_blocked_invariant(item)
        views._persist_direct_assignment_pin(
            item,
            'aoi@shizuha.com',
            'Distinct merger required after kai2574 review',
            actor_id=self.kei.id,
            actor_email='kei@shizuha.com',
        )

        def available(email):
            email = (email or '').lower()
            if email == 'aoi@shizuha.com':
                return True
            return email == 'merger@shizuha.com'

        with mock.patch.multiple(
            WorkflowService,
            _pod_roster=mock.Mock(return_value={
                'merge': ['aoi@shizuha.com', 'merger@shizuha.com'],
            }),
            _is_agent_available_for_assignment=mock.Mock(side_effect=available),
            _disabled_agent_emails=mock.Mock(return_value={'aoi@shizuha.com'}),
            _linked_pr_current_head_reviewers=mock.Mock(return_value=({'kai2574'}, [], 1)),
        ), mock.patch(
            'tasks.user_service.user_service.get_user_by_email',
            side_effect=self._user_by_email,
        ), mock.patch(
            'tasks.tasks._soft_unavailable_owner_protected', return_value=False,
        ), mock.patch.dict(
            'os.environ',
            {'PULSE_HEALTH_EXPORTER_URL': '', 'PULSE_API_HEALTH_URL': 'disabled'},
            clear=False,
        ):
            sweep_rebalance_pods.apply(args=[], kwargs={'max_skew': 0})

        item.refresh_from_db()
        self.assertEqual(item.status, 'blocked')
        self.assertEqual(item.assignee_email, 'aoi@shizuha.com')
        self.assertEqual(
            item.metadata['explicit_merge_handoff']['assignee_email'],
            'aoi@shizuha.com',
        )
