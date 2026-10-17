"""BKSH-25 / PLS-16: stale-review failover must never seat the operator.

personal-3::review was staffed with the CEO as its only member. The 48h
stale-review sweep then elected operator as Books ProjectTeamLead, and every
later team-route (including coordinator MCP, which authenticates as the CEO)
landed review work on the HO inbox.

These tests pin the production-order caller sequence:
  TeamRoster([operator]) + ProjectTeamLead(operator) + in_review item
  → resolver returns (None, None) and deactivates the lead
  → sweep_stale_reviews group-queues, never assigns the operator
  → POST /assign/ assignment_group=review as the operator actor group-queues
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from tasks.models import Comment, Item, Project, ProjectTeamLead, TeamRoster
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import sweep_stale_reviews


OPERATOR_EMAIL = 'operator@example.test'
OPERATOR_ID = 3
REVIEWER = 'rei@shizuha.com'
REVIEWER_ID = 106
User = get_user_model()


class _Users:
    users = {
        OPERATOR_EMAIL: OPERATOR_ID,
        REVIEWER: REVIEWER_ID,
        'shizuha@shizuha.com': 1,
    }

    def get_user_by_email(self, email):
        uid = self.users.get((email or '').strip().lower())
        return {'id': uid, 'email': email} if uid else None

    def get_user_by_id(self, user_id):
        for email, uid in self.users.items():
            if int(user_id) == uid:
                return {'id': uid, 'email': email}
        return None

    def resolve_user_info(self, user_id=None, email=None):
        if email:
            return self.get_user_by_email(email)
        if user_id:
            return self.get_user_by_id(user_id)
        return None


class StaleReviewOperatorFailoverTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Books',
            key='BKSH',
            org_id=4,
        )
        TeamRoster.objects.update_or_create(
            organization_id=4,
            team_name='review',
            defaults={
                'organization_slug': 'personal-3',
                'tiers': [[OPERATOR_EMAIL]],
                'is_active': True,
                'wip_enforced': False,
            },
        )
        self.lead = ProjectTeamLead.objects.create(
            org_id=4,
            project_key='bksh',
            team_name='review',
            lead_email=OPERATOR_EMAIL,
            lead_id=OPERATOR_ID,
            is_active=True,
        )
        self.item = Item.objects.create(
            title='voucher list 500',
            mode='task',
            item_key='BKSH-OP-1',
            status='in_review',
            priority='high',
            assignment_group='review',
            workflow_name='autonomous-bug',
            project=self.project,
            assignee_id=OPERATOR_ID,
            assignee_email=OPERATOR_EMAIL,
            created_by_email='sato@shizuha.com',
        )

    def _resolver(self, **kwargs):
        with mock.patch(
            'tasks.user_service.user_service', _Users(),
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_seat_satisfies_required_capability', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_new_assignment', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_assignment', return_value=True,
        ):
            return WorkflowService._resolve_team_assignee(
                'review',
                project_key='BKSH',
                item=self.item,
                enforce_cap=False,
                **kwargs,
            )

    def test_resolver_does_not_return_rostered_operator_for_review(self):
        uid, email = self._resolver()
        self.assertIsNone(uid)
        self.assertIsNone(email)
        # Operator directive 2026-09-17: leads are removed — the lead record is
        # inert for routing (never consulted, never deactivated). The
        # never-route-the-operator guarantee is enforced by the resolver's
        # operator guard, independent of lead records.
        self.lead.refresh_from_db()
        self.assertTrue(self.lead.is_active)

    def test_human_ops_still_elects_rostered_operator(self):
        TeamRoster.objects.update_or_create(
            organization_id=4,
            team_name='human-ops',
            defaults={
                'organization_slug': 'personal-3',
                'tiers': [[OPERATOR_EMAIL]],
                'is_active': True,
                'wip_enforced': False,
            },
        )
        item = Item.objects.create(
            title='physical hands',
            mode='task',
            status='todo',
            assignment_group='human-ops',
            workflow_name='simple',
            project=self.project,
        )
        with mock.patch(
            'tasks.user_service.user_service', _Users(),
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_seat_satisfies_required_capability', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_new_assignment', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_assignment', return_value=True,
        ):
            uid, email = WorkflowService._resolve_team_assignee(
                'human-ops', item=item, enforce_cap=False,
            )
        self.assertEqual(email, OPERATOR_EMAIL)
        self.assertEqual(uid, OPERATOR_ID)

    def test_sweep_group_queues_instead_of_assigning_operator(self):
        Item.objects.filter(pk=self.item.pk).update(
            updated_at=timezone.now() - timedelta(hours=49),
        )
        with mock.patch(
            'tasks.user_service.user_service', _Users(),
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_seat_satisfies_required_capability', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_new_assignment', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_assignment', return_value=True,
        ):
            result = sweep_stale_reviews.apply(kwargs={'hours': 48}).get()

        self.item.refresh_from_db()
        self.assertNotEqual(
            (self.item.assignee_email or '').strip().lower(), OPERATOR_EMAIL,
        )
        self.assertFalse(self.item.assignee_id)
        self.assertEqual(self.item.assignment_group, 'review')
        self.assertTrue((self.item.metadata or {}).get('stale_review_escalated'))
        self.assertGreaterEqual(result['healed'] + result['escalated'], 1)
        self.assertTrue(
            Comment.objects.filter(
                item=self.item,
                content__icontains='group-queued',
            ).exists()
        )
        self.assertFalse(
            Comment.objects.filter(
                item=self.item,
                content__icontains=OPERATOR_EMAIL,
            ).exists()
        )

    def test_assign_api_as_operator_actor_group_queues_review(self):
        operator = User.objects.create_user(
            username='hritik', email=OPERATOR_EMAIL, password='x',
        )
        client = APIClient()
        client.force_authenticate(user=operator)
        with mock.patch(
            'tasks.user_service.user_service', _Users(),
        ), mock.patch(
            'tasks.views.user_service', _Users(),
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_seat_satisfies_required_capability', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_new_assignment', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_is_agent_available_for_assignment', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_team_registered_for_item', return_value=True,
        ):
            response = client.post(
                f'/api/items/{self.item.id}/assign/',
                {'assignment_group': 'review'},
                format='json',
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.item.refresh_from_db()
        self.assertFalse(self.item.assignee_id)
        self.assertFalse((self.item.assignee_email or '').strip())
        self.assertEqual(self.item.assignment_group, 'review')
        self.assertTrue(response.data.get('queued_no_available_member'))
