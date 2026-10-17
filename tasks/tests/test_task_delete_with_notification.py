"""Deleting a task that already has an in-app notification must succeed."""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from tasks.models import Item, Notification, Project

User = get_user_model()


class TaskDeleteWithNotificationTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='owner',
            email='owner@example.com',
            password='testpass123',
            is_staff=True,
        )
        self.client.force_authenticate(user=self.user)
        self.project = Project.objects.create(
            name='Work', key='WORK', org_id=909, access_level='open', is_default=True,
        )

    def test_delete_removes_the_task_and_its_notification(self):
        item = Item.objects.create(
            mode='task',
            title='Welcome',
            item_key='WORK-1',
            project=self.project,
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )
        Notification.objects.create(
            user_id=self.user.id,
            item=item,
            notification_type='assignment',
            title='WORK-1: Assigned',
            message='Assigned',
        )
        response = self.client.delete(f'/api/tasks/{item.id}/')
        self.assertIn(response.status_code, (status.HTTP_204_NO_CONTENT, status.HTTP_200_OK))
        self.assertFalse(Item.objects.filter(pk=item.id).exists())
        self.assertFalse(Notification.objects.filter(item_id=item.id).exists())
