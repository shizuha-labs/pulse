"""PLS-641: exact-org Review -> Merge handoff is one atomic routing tuple."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import (
    Comment,
    Item,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService


class PLS641AtomicMergeHandoffTests(TestCase):
    ORG_ID = 2
    ORG_SLUG = 'shizuha-digital'

    def setUp(self):
        users = get_user_model()
        self.reviewer = users.objects.create_user(
            username='pls641-reviewer',
            email='ichi@shizuha.com',
            password='x',
        )
        self.merger = users.objects.create_user(
            username='pls641-merger',
            email='aoi@shizuha.com',
            password='x',
        )
        self.project = Project.objects.create(
            name='PLS641 Digital',
            key='P641',
            org_id=self.ORG_ID,
            access_level='open',
        )
        source = Status.objects.create(
            slug='pls641_in_review',
            name='PLS641 In Review',
            category='in_progress',
        )
        target, _ = Status.objects.get_or_create(
            slug='awaiting_merge',
            defaults={
                'name': 'Awaiting Merge',
                'category': 'in_progress',
            },
        )
        verification, _ = Status.objects.get_or_create(
            slug='verification',
            defaults={
                'name': 'Verification',
                'category': 'in_progress',
            },
        )
        self.workflow = Workflow.objects.create(
            name='PLS641 Atomic Merge',
            slug='pls641-atomic-merge',
            initial_status=source,
            is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow,
            status=source,
            position=0,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow,
            status=target,
            position=1,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow,
            status=verification,
            position=2,
        )
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Review Passed',
            from_status=source.slug,
            to_status=target.slug,
            position=0,
            post_functions=[{'type': 'assign_team', 'value': 'merge'}],
        )
        TeamRoster.objects.create(
            organization_id=self.ORG_ID,
            organization_slug=self.ORG_SLUG,
            team_name='review',
            tiers=[[self.reviewer.email]],
            is_active=True,
        )
        TeamRoster.objects.create(
            organization_id=self.ORG_ID,
            organization_slug=self.ORG_SLUG,
            team_name='merge',
            tiers=[[self.merger.email]],
            is_active=True,
        )
        WorkflowService._invalidate_team_roster_caches()

    def tearDown(self):
        WorkflowService._invalidate_team_roster_caches()
        WorkflowService._db_avail_cache = None
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None

    def _org(self, org_id):
        if int(org_id) == self.ORG_ID:
            return {'id': self.ORG_ID, 'slug': self.ORG_SLUG}
        return None

    def _item(self, **overrides):
        values = {
            'project': self.project,
            'item_key': 'P641-1',
            'title': 'Deploy paper CronJobs after reviewed merge',
            'description': (
                'After merge, apply the digest-pinned CronJobs to the QNT '
                'namespace and verify the rollout.'
            ),
            'mode': 'task',
            'workflow_name': self.workflow.slug,
            'status': 'pls641_in_review',
            'workflow_status': 'PLS641 In Review',
            'assignment_group': 'review',
            'assignee_id': self.reviewer.id,
            'assignee_email': self.reviewer.email,
            'created_by_id': 999,
            'created_by_email': 'hana@shizuha.com',
            'priority': 'high',
            'metadata': {
                'pull_requests': [{
                    'repo': 'shizuha-labs/qnt-data',
                    'number': 1,
                    'status': 'open',
                }],
            },
        }
        values.update(overrides)
        return Item.objects.create(**values)

    @staticmethod
    def _user_info(email):
        user = get_user_model().objects.filter(email=email).first()
        if not user:
            return None
        return {'id': user.id, 'email': user.email, 'username': user.username}

    def _patches(self):
        return (
            patch(
                'tasks.services.admin_service.admin_service.get_organization',
                side_effect=self._org,
            ),
            patch(
                'tasks.user_service.user_service.get_user_by_email',
                side_effect=self._user_info,
            ),
            patch.object(
                WorkflowService,
                '_is_agent_available_for_assignment',
                return_value=True,
            ),
            patch.object(
                WorkflowService,
                '_merge_sod_candidate_reasons',
                return_value=[],
            ),
            patch.object(
                WorkflowService,
                '_member_can_execute_from_status',
                return_value=True,
            ),
            patch.object(
                WorkflowService,
                '_agent_active_load',
                return_value=0,
            ),
            patch.object(
                WorkflowService,
                '_agent_workable_load',
                return_value=0,
            ),
        )

    def test_qnt_shaped_source_merge_routes_exact_org_merger(self):
        """Future deploy prose cannot make the source-only Merge seat need kubeconfig."""
        item = self._item()
        self.assertIn('kubeconfig', WorkflowService._task_required_capabilities(
            self._item(
                item_key='P641-VERIFY',
                status='verification',
                workflow_status='Verification',
                assignee_id=None,
                assignee_email='',
            )
        ))

        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            WorkflowService.execute_transition(
                item,
                'awaiting_merge',
                user=self.reviewer,
                transition_id=self.transition.id,
            )

        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')
        self.assertEqual(item.assignment_group, 'merge')
        self.assertEqual(item.assignee_id, self.merger.id)
        self.assertEqual(item.assignee_email, self.merger.email)
        self.assertNotEqual(item.assignee_email, self.reviewer.email)
        self.assertNotIn(
            'kubeconfig',
            WorkflowService._task_required_capabilities(item),
        )

    def test_capability_gap_merge_handoff_seats_available_member(self):
        """A missing capability still seats an available member of the destination team.

        The previous reviewer must not remain. The member reroutes if the work
        is not theirs; the handoff does not park the task with nobody assigned.
        """
        item = self._item(
            metadata={
                'required_capabilities': ['needs:kubeconfig'],
                'pull_requests': [{
                    'repo': 'shizuha-labs/qnt-data',
                    'number': 1,
                    'status': 'open',
                }],
            },
        )
        patches = self._patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], \
                patch.object(
                    WorkflowService,
                    '_seat_satisfies_required_capability',
                    return_value=False,
                ):
            WorkflowService.execute_transition(
                item,
                'awaiting_merge',
                user=self.reviewer,
                transition_id=self.transition.id,
            )

        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')
        self.assertEqual(item.assignment_group, 'merge')
        self.assertEqual(item.assignee_id, self.merger.id)
        self.assertEqual(item.assignee_email, self.merger.email)
        self.assertNotEqual(item.assignee_id, self.reviewer.id)
        holds = Comment.objects.filter(
            item=item,
            origin_event='resolver.capability_hold',
        )
        self.assertFalse(holds.exists())

    def test_unavailable_merge_handoff_clears_stale_reviewer(self):
        item = self._item()
        patches = self._patches()
        with patches[0], patches[1], \
                patch.object(
                    WorkflowService,
                    '_is_agent_available_for_assignment',
                    return_value=False,
                ), patches[3], patches[4], patches[5], patches[6]:
            WorkflowService.execute_transition(
                item,
                'awaiting_merge',
                user=self.reviewer,
                transition_id=self.transition.id,
            )

        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')
        self.assertEqual(item.assignment_group, 'merge')
        self.assertIsNone(item.assignee_id)
        self.assertFalse(item.assignee_email)
        holds = Comment.objects.filter(
            item=item,
            origin_event='resolver.routability_hold',
        )
        self.assertTrue(holds.exists())
        # PLS-721: with every seat unavailable, the legacy availability filter
        # drops all candidates AFTER the routability verdict allowed them
        # (shadow mode is the default) — zero seats are SCORED not-live, so the
        # hold now carries the truthful 'no_candidate_evidence' class instead
        # of the fabricated 'seat not live for new assignment' default. The
        # hold itself (and the group-queued handoff it drives) is unchanged.
        self.assertTrue(any(
            # reason_classes carries the _routability_reason_label LABEL, not the
            # raw slug (the pre-PLS-721 assertion used the label form too —
            # 'seat not live for new assignment').
            'no candidate evidence (zero seats scored)'
            in ((hold.origin_payload or {}).get('reason_classes') or [])
            for hold in holds
        ))
