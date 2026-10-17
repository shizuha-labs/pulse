"""
Tests that item-scoped endpoints accept both numeric PK and item_key (e.g. "AT-83").

MCP agents only ever see item_key values from pulse_get_task / pulse_list_tasks,
so endpoints must accept either form.
"""
import datetime

from rest_framework import status

from tasks.models import Attachment, Comment, Item, Project, WorkLog
from .base import AuthenticatedAPITestCase


class CommentItemKeyAcceptanceTests(AuthenticatedAPITestCase):
    """Comment create / list must accept both PK and item_key for the `item` field."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Acceptance Test',
            key='AT',
            org_id=1,
        )
        self.item = Item.objects.create(
            mode='task',
            title='Item-key acceptance',
            project=self.project,
            item_key='AT-9001',
            created_by_id=self.user.id,
        )

    def test_create_comment_with_numeric_pk(self):
        response = self.client.post(
            '/api/comments/',
            {'item': self.item.pk, 'content': 'by pk'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(Comment.objects.filter(item=self.item).count(), 1)

    def test_create_comment_with_item_key(self):
        response = self.client.post(
            '/api/comments/',
            {'item': self.item.item_key, 'content': 'by key'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(Comment.objects.filter(item=self.item).count(), 1)

    def test_create_comment_with_unknown_item_key_returns_400(self):
        response = self.client.post(
            '/api/comments/',
            {'item': 'AT-999999', 'content': 'bad key'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_comments_with_item_key(self):
        Comment.objects.create(
            item=self.item,
            content='hi',
            author_id=self.user.id,
            author_email=self.user.email,
        )
        response = self.client.get('/api/comments/', {'item': self.item.item_key})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        results = data.get('results', data if isinstance(data, list) else [])
        self.assertEqual(len(results), 1)

    def test_list_comments_with_numeric_pk(self):
        Comment.objects.create(
            item=self.item,
            content='hi',
            author_id=self.user.id,
            author_email=self.user.email,
        )
        response = self.client.get('/api/comments/', {'item': self.item.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        results = data.get('results', data if isinstance(data, list) else [])
        self.assertEqual(len(results), 1)

    def test_list_comments_default_page_includes_thread_over_ten_comments(self):
        for index in range(11):
            Comment.objects.create(
                item=self.item,
                content=f'comment {index}',
                author_id=self.user.id,
                author_email=self.user.email,
            )

        response = self.client.get('/api/comments/', {'item': self.item.item_key})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        results = data.get('results', data if isinstance(data, list) else [])
        self.assertEqual(data.get('count'), 11)
        self.assertEqual(len(results), 11)

    def test_create_reply_with_item_key(self):
        """Parent-comment validation must also tolerate item_key on the create payload."""
        parent = Comment.objects.create(
            item=self.item,
            content='parent',
            author_id=self.user.id,
            author_email=self.user.email,
        )
        response = self.client.post(
            '/api/comments/',
            {'item': self.item.item_key, 'content': 'child', 'parent': parent.pk},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)


class ItemLookupAcceptanceTests(AuthenticatedAPITestCase):
    """Item detail lookup must accept UUID, project key, numeric-only key, and #display key."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Acceptance Test',
            key='AT',
            org_id=1,
        )
        self.item = Item.objects.create(
            mode='task',
            title='Numeric-key item',
            project=self.project,
            item_key='6559',
            created_by_id=self.user.id,
        )

    def test_get_item_with_numeric_only_item_key(self):
        response = self.client.get('/api/items/6559/')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json()['item_key'], '6559')

    def test_get_item_with_display_key(self):
        response = self.client.get('/api/items/%236559/')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json()['item_key'], '6559')

    def test_get_item_with_uuid_pk(self):
        response = self.client.get(f'/api/items/{self.item.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertEqual(response.json()['item_key'], '6559')


class AttachmentListItemKeyAcceptanceTests(AuthenticatedAPITestCase):
    """Attachment list endpoint must accept item_key in the `?item=` filter."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Acceptance Test',
            key='AT',
            org_id=1,
        )
        self.item = Item.objects.create(
            mode='task',
            title='Attachment host',
            project=self.project,
            item_key='AT-9101',
            created_by_id=self.user.id,
        )
        self.attachment = Attachment.objects.create(
            item=self.item,
            storage_key='x/test.bin',
            filename='test.bin',
            file_type='application/octet-stream',
            file_size=1,
            uploaded_by_id=self.user.id,
            uploaded_by_email=self.user.email,
        )

    def test_list_attachments_with_item_key(self):
        response = self.client.get('/api/attachments/', {'item': self.item.item_key})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        results = data.get('results', data if isinstance(data, list) else [])
        self.assertEqual(len(results), 1)
        self.assertEqual(str(results[0]['id']), str(self.attachment.id))

    def test_list_attachments_with_numeric_pk(self):
        response = self.client.get('/api/attachments/', {'item': self.item.pk})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        results = data.get('results', data if isinstance(data, list) else [])
        self.assertEqual(len(results), 1)


class WorkLogItemKeyAcceptanceTests(AuthenticatedAPITestCase):
    """WorkLog endpoints must accept both PK and item_key on create, list, and by_item."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Acceptance Test',
            key='AT',
            org_id=1,
        )
        self.item = Item.objects.create(
            mode='task',
            title='Time target',
            project=self.project,
            item_key='AT-9201',
            created_by_id=self.user.id,
        )

    def test_create_work_log_with_item_key(self):
        response = self.client.post(
            '/api/work-logs/',
            {
                'item': self.item.item_key,
                'duration_minutes': 30,
                'worked_date': datetime.date.today().isoformat(),
                'description': 'by key',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        self.assertEqual(WorkLog.objects.filter(item=self.item).count(), 1)

    def test_create_work_log_with_numeric_pk(self):
        response = self.client.post(
            '/api/work-logs/',
            {
                'item': self.item.pk,
                'duration_minutes': 30,
                'worked_date': datetime.date.today().isoformat(),
                'description': 'by pk',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)

    def test_list_work_logs_with_item_key(self):
        WorkLog.objects.create(
            item=self.item,
            user_id=self.user.id,
            user_email=self.user.email,
            duration_minutes=15,
            worked_date=datetime.date.today(),
        )
        response = self.client.get('/api/work-logs/', {'item': self.item.item_key})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        results = data.get('results', data if isinstance(data, list) else [])
        self.assertEqual(len(results), 1)

    def test_by_item_action_with_item_key(self):
        WorkLog.objects.create(
            item=self.item,
            user_id=self.user.id,
            user_email=self.user.email,
            duration_minutes=45,
            worked_date=datetime.date.today(),
        )
        response = self.client.get('/api/work-logs/by_item/', {'item': self.item.item_key})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()['total_minutes'], 45)

    def test_by_item_action_with_unknown_key_returns_404(self):
        response = self.client.get('/api/work-logs/by_item/', {'item': 'AT-999999'})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
