"""PLS-194 regression coverage for transition knowledge-article corruption."""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from tasks.models import (
    Item, Project, Status, Workflow, WorkflowStatus, WorkflowTransition,
)


class TransitionKnowledgeArticleRegressionTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='pls194',
            email='pls194@example.com',
            password='testpass123',
            is_staff=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.awaiting_merge, _ = Status.objects.get_or_create(
            slug='awaiting_merge',
            defaults={
                'name': 'Awaiting Merge',
                'category': 'in_progress',
                'position': 1,
            },
        )
        self.done, _ = Status.objects.get_or_create(
            slug='done',
            defaults={'name': 'Done', 'category': 'done', 'position': 2},
        )
        self.workflow = Workflow.objects.create(
            name='Autonomous Dev PLS194',
            slug='autonomous-dev-pls194',
            initial_status=self.awaiting_merge,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow, status=self.awaiting_merge, position=1
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow, status=self.done, position=2
        )
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Merge',
            from_status='awaiting_merge',
            to_status='done',
            knowledge_articles=[{'type': 'inline', 'title': 'Old', 'content': 'old'}],
        )
        self.project = Project.objects.create(name='PLS194 Project', key='P194', org_id=1)
        self.item = Item.objects.create(
            mode='task',
            title='Awaiting merge task',
            project=self.project,
            item_key='P194-1',
            status='awaiting_merge',
            workflow_name=self.workflow.name,
            created_by_id=self.user.id,
        )

    def test_transition_patch_parses_json_string_articles(self):
        articles = [
            {'type': 'inline', 'title': 'Parsed', 'content': 'from MCP string'}
        ]

        response = self.client.patch(
            f'/api/transitions/{self.transition.id}/',
            {'knowledge_articles': json.dumps(articles)},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.transition.refresh_from_db()
        self.assertEqual(self.transition.knowledge_articles, articles)
        self.assertEqual(response.data['knowledge_articles'], articles)

    def test_transition_patch_rejects_malformed_articles(self):
        response = self.client.patch(
            f'/api/transitions/{self.transition.id}/',
            {'knowledge_articles': 'not json'},
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.transition.refresh_from_db()
        self.assertIsInstance(self.transition.knowledge_articles, list)

    def test_available_transitions_sanitizes_legacy_raw_string_row(self):
        self.transition.knowledge_articles = 'raw malformed string'
        self.transition.save(update_fields=['knowledge_articles'])

        response = self.client.get(f'/api/items/{self.item.item_key}/available-transitions/')

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['transitions'][0]['knowledge_articles'], [])

    def test_execute_transition_sanitizes_executed_and_next_article_payloads(self):
        self.transition.knowledge_articles = 'raw malformed string'
        self.transition.save(update_fields=['knowledge_articles'])
        next_transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Reopen',
            from_status='done',
            to_status='awaiting_merge',
            knowledge_articles='also malformed',
        )

        response = self.client.post(
            f'/api/items/{self.item.item_key}/execute-transition/',
            {'transition_id': str(self.transition.id)},
            format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['executed_transition']['knowledge_articles'], [])
        rendered_next = [
            t for t in response.data['next_transitions']
            if t['id'] == str(next_transition.id)
        ]
        self.assertEqual(rendered_next[0]['knowledge_articles'], [])
