"""Human asks are workable roots. waiting_external cannot be entered.

The settlement endpoint remains for a legacy parked row. New rows never
enter that status, so the endpoint reports that no wait is active. A todo
blocker child holds its parent until a block-releasing status.
"""
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService
from .base import create_blocked_item


class HumanWaitRetiredTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='holder', email='holder@example.com', password='x',
        )
        self.user.organization_memberships = {1: 'member'}
        self.client.force_authenticate(self.user)
        membership = patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            side_effect=lambda uid, org, *args, **kwargs: org == 1 and uid == self.user.pk,
        )
        membership.start()
        self.addCleanup(membership.stop)
        self.project = Project.objects.create(
            name='Settlement', key='HWS', org_id=1, access_level='open',
        )
        TeamRoster.objects.create(
            organization_id=1, organization_slug='shizuha', team_name='settlement-test',
            tiers=[[self.user.email, 'engineer@example.com']],
        )
        self.child = Item.objects.create(
            title='Human prerequisite', project=self.project, mode='task',
            workflow_name='simple', status='todo', assignee_id=self.user.pk,
            assignee_email=self.user.email, assignment_group='settlement-test',
        )
        self.parent = create_blocked_item(
            blocker=self.child, title='Dependent implementation', project=self.project,
            workflow_name='simple', mode='task', pre_block_status='todo',
            assignee_id=70001, assignee_email='engineer@example.com',
            assignment_group='settlement-test',
        )

    def test_entry_is_refused_and_the_root_stays_workable(self):
        self.child.status = 'waiting_external'
        with self.assertRaisesMessage(ValidationError, 'waiting_external_retired'):
            self.child.save(update_fields=['status'])
        self.child.refresh_from_db()
        self.parent.refresh_from_db()
        self.assertEqual(self.child.status, 'todo')
        self.assertEqual(self.parent.status, 'blocked')
        self.assertTrue(WorkflowService._active_blockers_for(self.parent))

    def test_settle_endpoint_refuses_a_workable_root(self):
        response = self.client.post(
            f'/api/items/{self.child.pk}/settle-human-wait/',
            {
                'receipt': str(uuid4()),
                'wait_generation': 1,
                'transition_id': str(uuid4()),
                'summary': 'The ask is ordinary work now.',
                'resolutions': [{
                    'parent_id': self.parent.pk,
                    'comment_id': 1,
                    'sha256': 'a' * 64,
                }],
            },
            format='json',
        )
        self.assertEqual(response.status_code, 409, getattr(response, 'data', None))
        self.assertEqual(response.data['code'], 'human_wait_generation_stale')
        self.child.refresh_from_db()
        self.parent.refresh_from_db()
        self.assertEqual(self.child.status, 'todo')
        self.assertEqual(self.parent.status, 'blocked')
