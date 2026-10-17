"""PLAT-4195 residual child-resource authorization regressions."""
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient, APITestCase

from tasks.models import (
    CustomField, CustomFieldValue, Item, Project, ProjectMembership, WorkLog,
)

User = get_user_model()


class PLAT4195ChildMutationAuthorizationTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner_a = User.objects.create_user(
            username='owner_a', email='owner-a@sec.test', password='x')
        self.owner_a.organization_memberships = {1: 'owner'}
        self.owner_b = User.objects.create_user(
            username='owner_b', email='owner-b@sec.test', password='x')
        self.owner_b.organization_memberships = {2: 'owner'}
        self.viewer_a = User.objects.create_user(
            username='viewer_a', email='viewer-a@sec.test', password='x')
        self.viewer_a.organization_memberships = {1: 'member'}
        self.editor_a = User.objects.create_user(
            username='editor_a', email='editor-a@sec.test', password='x')
        self.editor_a.organization_memberships = {1: 'member'}

        self.project_a = Project.objects.create(
            name='Tenant A', key='TENA', org_id=1,
            access_level='private', is_active=True)
        self.project_b = Project.objects.create(
            name='Tenant B', key='TENB', org_id=2,
            access_level='private', is_active=True)
        ProjectMembership.objects.create(
            project=self.project_a, user_id=self.viewer_a.id,
            user_email=self.viewer_a.email, role='viewer')
        ProjectMembership.objects.create(
            project=self.project_a, user_id=self.editor_a.id,
            user_email=self.editor_a.email, role='member')

        self.item_a = Item.objects.create(
            mode='task', title='A item', project=self.project_a,
            item_key='TENA-1', created_by_id=self.owner_a.id)
        self.item_a2 = Item.objects.create(
            mode='task', title='A second', project=self.project_a,
            item_key='TENA-2', created_by_id=self.owner_a.id)
        self.item_b = Item.objects.create(
            mode='task', title='B secret', project=self.project_b,
            item_key='TENB-1', created_by_id=self.owner_b.id)

        self.log_a = WorkLog.objects.create(
            item=self.item_a, user_id=self.owner_a.id,
            user_email=self.owner_a.email, duration_minutes=10,
            worked_date='2026-07-11')
        self.log_b = WorkLog.objects.create(
            item=self.item_b, user_id=self.owner_b.id,
            user_email=self.owner_b.email, duration_minutes=20,
            worked_date='2026-07-11')
        # Simulates a row planted through the vulnerable create path before the
        # fix.  Log ownership must not keep the foreign parent readable/writable.
        self.planted_log = WorkLog.objects.create(
            item=self.item_b, user_id=self.owner_a.id,
            user_email=self.owner_a.email, duration_minutes=7,
            worked_date='2026-07-11', description='planted')

        self.field = CustomField.objects.create(
            name='Security note', slug='plat4195-security-note',
            field_type='text')
        self.field_2 = CustomField.objects.create(
            name='Second field', slug='plat4195-second-field',
            field_type='text')
        self.value_a = CustomFieldValue.objects.create(
            item=self.item_a, custom_field=self.field,
            value='A value', value_text='A value')
        self.value_b = CustomFieldValue.objects.create(
            item=self.item_b, custom_field=self.field,
            value='B secret value', value_text='B secret value')

    def _as(self, user):
        self.client.force_authenticate(user)

    def test_worklog_foreign_create_has_zero_side_effects(self):
        self._as(self.owner_a)
        self.item_b.refresh_from_db()
        before_minutes = self.item_b.time_spent_minutes
        before_count = WorkLog.objects.filter(item=self.item_b).count()
        response = self.client.post('/api/work-logs/', {
            'item': self.item_b.item_key, 'duration_minutes': 99,
            'remaining_estimate_minutes': 1, 'worked_date': '2026-07-11',
        }, format='json')
        self.assertEqual(response.status_code, 404)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.time_spent_minutes, before_minutes)
        self.assertEqual(WorkLog.objects.filter(item=self.item_b).count(), before_count)

    def test_planted_foreign_own_log_is_not_readable_or_deletable(self):
        self._as(self.owner_a)
        self.assertEqual(
            self.client.get(f'/api/work-logs/{self.planted_log.id}/').status_code,
            404)
        mine = self.client.get('/api/work-logs/my_logs/')
        self.assertEqual(mine.status_code, 200)
        ids = {str(row['id']) for row in mine.data['work_logs']}
        self.assertNotIn(str(self.planted_log.id), ids)
        before = self.item_b.time_spent_minutes
        self.assertEqual(
            self.client.delete(f'/api/work-logs/{self.planted_log.id}/').status_code,
            404)
        self.assertTrue(WorkLog.objects.filter(pk=self.planted_log.pk).exists())
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.time_spent_minutes, before)

    def test_worklog_reparent_is_immutable_and_foreign_target_fails_closed(self):
        self._as(self.owner_a)
        response = self.client.patch(f'/api/work-logs/{self.log_a.id}/', {
            'item': self.item_b.id,
        }, format='json')
        self.assertEqual(response.status_code, 404)
        self.log_a.refresh_from_db()
        self.assertEqual(self.log_a.item_id, self.item_a.id)

        response = self.client.patch(f'/api/work-logs/{self.log_a.id}/', {
            'item': self.item_a2.id,
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.log_a.refresh_from_db()
        self.assertEqual(self.log_a.item_id, self.item_a.id)

    def test_worklog_editor_positive_and_viewer_write_denied(self):
        self._as(self.owner_a)
        created = self.client.post('/api/work-logs/', {
            'item': self.item_a.item_key, 'duration_minutes': 5,
            'worked_date': '2026-07-11', 'description': 'positive-control',
        }, format='json')
        self.assertEqual(created.status_code, 201, created.content)
        log_id = WorkLog.objects.get(
            item=self.item_a, description='positive-control').id
        self.assertEqual(self.client.patch(
            f'/api/work-logs/{log_id}/', {'duration_minutes': 8},
            format='json').status_code, 200)
        self.assertEqual(self.client.delete(
            f'/api/work-logs/{log_id}/').status_code, 204)

        self._as(self.viewer_a)
        denied = self.client.post('/api/work-logs/', {
            'item': self.item_a.id, 'duration_minutes': 5,
            'worked_date': '2026-07-11',
        }, format='json')
        self.assertEqual(denied.status_code, 404)

    def test_custom_value_foreign_reads_and_all_writes_fail_closed(self):
        self._as(self.owner_a)
        self.assertEqual(self.client.get(
            f'/api/custom-field-values/{self.value_b.id}/').status_code, 404)
        # Existing (item, field) pair exercises the pre-serializer auth gate:
        # uniqueness validation must not disclose the foreign row with a 400.
        create = self.client.post('/api/custom-field-values/', {
            'item': self.item_b.id, 'custom_field': str(self.field.id),
            'value': 'overwrite',
        }, format='json')
        self.assertEqual(create.status_code, 404)
        self.assertEqual(self.client.patch(
            f'/api/custom-field-values/{self.value_b.id}/',
            {'value': 'changed'}, format='json').status_code, 404)
        self.assertEqual(self.client.delete(
            f'/api/custom-field-values/{self.value_b.id}/').status_code, 404)
        self.value_b.refresh_from_db()
        self.assertEqual(self.value_b.value, 'B secret value')

    def test_custom_value_reparent_requires_both_parents_and_editor_positive(self):
        self._as(self.owner_a)
        denied = self.client.patch(
            f'/api/custom-field-values/{self.value_a.id}/',
            {'item': self.item_b.id}, format='json')
        self.assertEqual(denied.status_code, 404)
        self.value_a.refresh_from_db()
        self.assertEqual(self.value_a.item_id, self.item_a.id)

        allowed = self.client.patch(
            f'/api/custom-field-values/{self.value_a.id}/',
            {'item': self.item_a2.id}, format='json')
        self.assertEqual(allowed.status_code, 200, allowed.content)
        self.value_a.refresh_from_db()
        self.assertEqual(self.value_a.item_id, self.item_a2.id)

    def test_custom_value_viewer_can_read_but_cannot_mutate(self):
        self._as(self.viewer_a)
        self.assertEqual(self.client.get(
            f'/api/custom-field-values/{self.value_a.id}/').status_code, 200)
        self.assertEqual(self.client.patch(
            f'/api/custom-field-values/{self.value_a.id}/',
            {'value': 'viewer write'}, format='json').status_code, 404)
        self.assertEqual(self.client.delete(
            f'/api/custom-field-values/{self.value_a.id}/').status_code, 404)
        self.assertEqual(self.client.post('/api/custom-field-values/', {
            'item': self.item_a.id, 'custom_field': str(self.field_2.id),
            'value': 'viewer create',
        }, format='json').status_code, 404)

    def test_explicit_project_editor_positive_controls(self):
        """A non-admin editor retains both child-resource write paths."""
        self._as(self.editor_a)
        work = self.client.post('/api/work-logs/', {
            'item': self.item_a.id, 'duration_minutes': 3,
            'worked_date': '2026-07-11', 'description': 'editor-positive',
        }, format='json')
        self.assertEqual(work.status_code, 201, work.content)
        value = self.client.post('/api/custom-field-values/', {
            'item': self.item_a.id, 'custom_field': str(self.field_2.id),
            'value': 'editor value',
        }, format='json')
        self.assertEqual(value.status_code, 201, value.content)
        value_id = CustomFieldValue.objects.get(
            item=self.item_a, custom_field=self.field_2).id
        self.assertEqual(self.client.patch(
            f'/api/custom-field-values/{value_id}/',
            {'value': 'editor updated'}, format='json').status_code, 200)
        self.assertEqual(self.client.delete(
            f'/api/custom-field-values/{value_id}/').status_code, 204)
