"""Team Tasks must count in SQL and must not call Identity per assignee."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from tasks.models import Item, Project

User = get_user_model()


class TeamTasksSummaryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            username='hritik',
            email='owner@example.test',
            password='x',
        )
        self.project = Project.objects.create(
            name='Hive', key='HIVE', org_id=1, access_level='private',
        )
        Item.objects.create(
            project=self.project, item_key='HIVE-1', mode='task', title='One',
            status='pending', assignee_id=12, assignee_email='sara@shizuha.com',
            assignment_group='engineering',
        )
        Item.objects.create(
            project=self.project, item_key='HIVE-2', mode='task', title='Two',
            status='pending', assignee_id=12, assignee_email='Sara@shizuha.com',
            assignment_group='engineering',
        )
        Item.objects.create(
            project=self.project, item_key='HIVE-3', mode='task', title='Three',
            status='pending', assignee_id=9, assignee_email='kai@shizuha.com',
            assignment_group='',
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @patch('tasks.user_service.user_service.get_user_by_id', side_effect=AssertionError('identity'))
    def test_summary_groups_without_identity_or_task_bodies(self, _identity):
        response = self.client.get('/api/items/team_tasks/', {'organization': 1})
        self.assertEqual(response.status_code, 200, response.data)
        members = {row['assignee_email'].lower(): row for row in response.data['team_members']}
        self.assertEqual(members['sara@shizuha.com']['task_count'], 2)
        self.assertEqual(members['sara@shizuha.com']['tasks'], [])
        self.assertEqual(members['kai@shizuha.com']['task_count'], 1)
        self.assertGreater(
            members['sara@shizuha.com']['task_count'],
            members['kai@shizuha.com']['task_count'],
        )

    @patch('tasks.user_service.user_service.get_user_by_id', side_effect=AssertionError('identity'))
    def test_assignee_detail_returns_that_persons_tasks(self, _identity):
        response = self.client.get('/api/items/team_tasks/', {
            'organization': 1,
            'assignee_email': 'sara@shizuha.com',
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['task_count'], 2)
        self.assertEqual(
            {row['item_key'] for row in response.data['tasks']},
            {'HIVE-1', 'HIVE-2'},
        )
        self.assertFalse(response.data['truncated'])
