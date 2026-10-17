from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from tasks.models import Item, KeyResult, Objective, Project


User = get_user_model()


class PLS272ObjectivesAuthTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='org1-user',
            email='org1@example.com',
            password='testpass123',
            is_staff=False,
        )
        self.user.organization_memberships = {1: 'member'}
        self.client.force_authenticate(user=self.user)

        self.project1 = Project.objects.create(name='Org One', key='OONE', org_id=1, is_active=True)
        self.project2 = Project.objects.create(name='Org Two', key='OTWO', org_id=2, is_active=True)
        self.item1 = Item.objects.create(title='Org one work', project=self.project1, item_key='OONE-1')
        self.item2 = Item.objects.create(title='Org two work', project=self.project2, item_key='OTWO-1')
        self.objective1 = Objective.objects.create(org_id=1, project=self.project1, title='Org one objective')
        self.objective2 = Objective.objects.create(org_id=2, project=self.project2, title='Org two objective')

    def test_objective_create_rejects_org_outside_memberships(self):
        response = self.client.post('/api/objectives/', {
            'org_id': 2,
            'title': 'Cross-org write',
            'status': 'active',
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Objective.objects.filter(title='Cross-org write').exists())

    def test_objective_create_rejects_cross_org_project_and_item_links(self):
        response = self.client.post('/api/objectives/', {
            'org_id': 1,
            'title': 'Cross-org links',
            'project_ids': [self.project2.id],
            'item_ids': [self.item2.id],
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Objective.objects.filter(title='Cross-org links').exists())

    def test_objective_update_rejects_cross_org_links_without_side_effects(self):
        response = self.client.patch(f'/api/objectives/{self.objective1.id}/', {
            'project_ids': [self.project2.id],
            'item_ids': [self.item2.id],
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.objective1.refresh_from_db()
        self.assertEqual(list(self.objective1.projects.all()), [])
        self.assertEqual(list(self.objective1.items.all()), [])

    def test_objective_update_rejects_org_id_changes(self):
        response = self.client.patch(f'/api/objectives/{self.objective1.id}/', {
            'org_id': 2,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.objective1.refresh_from_db()
        self.assertEqual(self.objective1.org_id, 1)

    def test_key_result_create_rejects_objective_outside_memberships(self):
        response = self.client.post('/api/key-results/', {
            'objective': self.objective2.id,
            'title': 'Pollute org two objective',
            'metric_name': 'bad writes',
            'target_value': '1.00',
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(KeyResult.objects.filter(title='Pollute org two objective').exists())

    def test_key_result_update_rejects_cross_org_objective_without_side_effects(self):
        key_result = KeyResult.objects.create(objective=self.objective1, title='Org one KR')

        response = self.client.patch(f'/api/key-results/{key_result.id}/', {
            'objective': self.objective2.id,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        key_result.refresh_from_db()
        self.assertEqual(key_result.objective_id, self.objective1.id)

    def test_key_result_rejects_confidence_above_100(self):
        response = self.client.post('/api/key-results/', {
            'objective': self.objective1.id,
            'title': 'Invalid confidence',
            'target_value': '1.00',
            'confidence': 101,
        }, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(KeyResult.objects.filter(title='Invalid confidence').exists())
