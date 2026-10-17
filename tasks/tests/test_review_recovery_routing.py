"""Regression coverage for transient reviewer outages and queue recovery.

A runtime rollout briefly hid Revi from the health snapshot. Pulse permanently
deactivated every Revi ProjectTeamLead row and assigned the review lane's queued
work to Reika; membership mode then refused to rebalance any of it after Revi
recovered. These tests pin the intended team-first contract:

* transient health changes spill one assignment without rewriting durable lead
  affinity; and
* untouched review-lane items rebalance across recovered team members while a
  review whose current owner has started work stays put.
"""
from unittest import mock

from django.test import TestCase

from tasks.models import Item, ItemActivity, Project, ProjectTeamLead, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import sweep_rebalance_pods


REVI = 'revi@shizuha.com'
REIKA = 'reika@shizuha.com'


class _Users:
    users = {
        REVI: 701,
        REIKA: 702,
    }

    def get_user_by_email(self, email):
        uid = self.users.get((email or '').strip().lower())
        return {'id': uid, 'email': email} if uid else None

    def get_user_by_id(self, user_id):
        for email, uid in self.users.items():
            if int(user_id) == uid:
                return {'id': uid, 'email': email}
        return None


class ReviewRecoveryRoutingTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Review recovery routing',
            key='RRT',
            org_id=1,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='review',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[REVI, REIKA]],
                'is_active': True,
                'wip_enforced': False,
            },
        )

    @mock.patch(
        'tasks.services.workflow_service.WorkflowService.'
        '_member_can_execute_from_status',
        return_value=True,
    )
    @mock.patch(
        'tasks.services.workflow_service.WorkflowService.'
        '_seat_satisfies_required_capability',
        return_value=True,
    )
    def test_transiently_unavailable_lead_spills_without_deactivation(
        self,
        _capable,
        _can_execute,
    ):
        lead = ProjectTeamLead.objects.create(
            org_id=1,
            project_key='rrt',
            team_name='review',
            lead_email=REVI,
            lead_id=701,
            is_active=True,
        )
        item = Item.objects.create(
            title='urgent review submitted during rollout',
            mode='task',
            status='in_review',
            priority='urgent',
            assignment_group='review',
            workflow_name='autonomous-bug',
            project=self.project,
        )

        with mock.patch(
            'tasks.user_service.user_service',
            _Users(),
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            side_effect=lambda email: (email or '').strip().lower() == REIKA,
        ):
            uid, email = WorkflowService._resolve_team_assignee(
                'review',
                project_key='RRT',
                item=item,
                enforce_cap=False,
            )

        lead.refresh_from_db()
        # Operator directive 2026-09-17: leads are removed as a concept — the
        # resolver no longer prefers (or spills from) a lead. The only
        # available peer (reika) is assigned by the uniform least-loaded rule,
        # and the ProjectTeamLead record is inert: never consulted, never
        # mutated by routing.
        self.assertEqual((uid, email), (702, REIKA))
        self.assertTrue(lead.is_active)
        self.assertEqual(lead.lead_email, REVI)
        self.assertEqual(lead.deactivated_reason, '')

    @mock.patch.dict(
        'os.environ',
        {
            'PULSE_API_HEALTH_URL': 'disabled',
            'PULSE_HEALTH_EXPORTER_URL': 'disabled',
            'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
        },
    )
    def test_membership_mode_rebalances_untouched_review_queue_and_preserves_started(
        self,
    ):
        items = []
        for title, priority in (
            ('PLAT urgent blocker review', 'urgent'),
            ('ordinary queued review one', 'normal'),
            ('ordinary queued review two', 'normal'),
            ('review already started by Reika', 'normal'),
        ):
            item = Item.objects.create(
                title=title,
                mode='task',
                status='in_review',
                priority=priority,
                assignment_group='review',
                assignee_id=702,
                assignee_email=REIKA,
                workflow_name='autonomous-bug',
                project=self.project,
            )
            ItemActivity.objects.create(
                item=item,
                action='assigned',
                user_id=0,
                user_email='system@pulse',
            )
            items.append(item)

        engaged = items[-1]
        ItemActivity.objects.create(
            item=engaged,
            action='commented',
            user_id=702,
            user_email=REIKA,
        )

        assignment_delay = mock.Mock()
        with self.captureOnCommitCallbacks(execute=True), mock.patch(
            'tasks.user_service.user_service',
            _Users(),
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_member_can_execute_from_status',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_seat_satisfies_required_capability',
            return_value=True,
        ), mock.patch(
            'tasks.services.membership_rebalancer.rebalancer_enabled',
            return_value=True,
        ), mock.patch(
            'tasks.services.membership_rebalancer.rebalance_memberships',
            return_value={'enabled': True, 'expired': 0, 'added': [], 'skipped': []},
        ), mock.patch(
            'tasks.tasks.send_item_notification.delay',
            new=assignment_delay,
        ), mock.patch(
            'tasks.tasks.notify_comment_mentions.delay',
            return_value=None,
        ):
            result = sweep_rebalance_pods.run(max_skew=0)

        for item in items:
            item.refresh_from_db()

        self.assertEqual(result['moves'], 2)
        self.assertEqual(items[0].assignee_email, REVI)
        self.assertEqual(
            sorted(item.assignee_email for item in items),
            [REIKA, REIKA, REVI, REVI],
        )
        self.assertEqual(engaged.assignee_email, REIKA)
        self.assertEqual(
            ItemActivity.objects.filter(
                item=engaged,
                action='assigned',
                user_email='system@pulse',
            ).count(),
            1,
        )
        assignment_delay.assert_not_called()
