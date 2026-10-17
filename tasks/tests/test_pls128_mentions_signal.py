from unittest.mock import patch

from rest_framework import status

from tasks.models import Comment, Item, Project
from tasks.tests.base import AuthenticatedAPITestCase


class CommentMentionSignalDispatchTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(name='Mention Dispatch', key='MD', org_id=1)
        self.item = Item.objects.create(
            mode='task',
            title='Mention dispatch task',
            project=self.project,
            item_key='MD-1',
            workflow_name='simple',
            created_by_id=self.user.id,
        )

    def test_view_created_comment_enqueues_mentions_once_via_signal(self):
        with patch('tasks.tasks.notify_comment_mentions.delay') as delay:
            response = self.client.post(
                '/api/comments/',
                {'item': self.item.pk, 'content': 'please check @hana'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        comment = Comment.objects.get(item=self.item)
        delay.assert_called_once_with(comment.id)

    def test_view_created_comment_without_mention_does_not_enqueue(self):
        with patch('tasks.tasks.notify_comment_mentions.delay') as delay:
            response = self.client.post(
                '/api/comments/',
                {'item': self.item.pk, 'content': 'no ping here'},
                format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        delay.assert_not_called()

    def test_editing_existing_comment_with_mention_does_not_enqueue_again(self):
        comment = Comment.objects.create(
            item=self.item,
            author_id=self.user.id,
            author_email=self.user.email,
            content='initial note',
        )

        with patch('tasks.tasks.notify_comment_mentions.delay') as delay:
            comment.content = 'edited note @hana'
            comment.save(update_fields=['content'])

        delay.assert_not_called()
