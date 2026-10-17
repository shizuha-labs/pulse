"""PLAT-4195: visibility must not be confused with bulk mutation authority."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient, APITestCase

from tasks.models import Item, Project, ProjectMembership, Sprint
from tasks.services.permissions import PermissionService


User = get_user_model()


class PLAT4195BulkMutationAuthorizationTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.actor = User.objects.create_user(
            username='bulk_member', email='bulk-member@sec.test', password='x')
        self.actor.organization_memberships = {1: 'member'}
        self.owner = User.objects.create_user(
            username='bulk_owner', email='bulk-owner@sec.test', password='x')
        self.org_admin = User.objects.create_user(
            username='bulk_admin', email='bulk-admin@sec.test', password='x')
        self.org_admin.organization_memberships = {1: 'owner'}
        self.staff = User.objects.create_user(
            username='bulk_staff', email='bulk-staff@sec.test', password='x', is_staff=True)
        # Exercise the is_org_admin_or_owner(superuser) branch independently of
        # the early is_staff escape hatch.
        self.nonstaff_superuser = User.objects.create_user(
            username='bulk_root', email='bulk-root@sec.test', password='x',
            is_staff=False, is_superuser=True,
        )

        self.viewer_project = Project.objects.create(
            name='Viewer private', key='BVIEW', org_id=1,
            access_level='private', is_active=True,
        )
        self.viewer_membership = ProjectMembership.objects.create(
            project=self.viewer_project,
            user_id=self.actor.id,
            user_email=self.actor.email,
            role='viewer',
        )
        self.viewer_sprint = Sprint.objects.create(
            project=self.viewer_project, name='Viewer sprint')

        self.open_project = Project.objects.create(
            name='Open project', key='BOPEN', org_id=1,
            access_level='open', is_active=True,
        )
        self.project_a = Project.objects.create(
            name='Editable A', key='BPA', org_id=1,
            access_level='private', is_active=True,
        )
        self.project_b = Project.objects.create(
            name='Editable B', key='BPB', org_id=1,
            access_level='private', is_active=True,
        )
        self.foreign_project = Project.objects.create(
            name='Foreign project', key='BFRN', org_id=2,
            access_level='private', is_active=True,
        )
        self.sprint_a = Sprint.objects.create(project=self.project_a, name='Sprint A')
        self.sprint_b = Sprint.objects.create(project=self.project_b, name='Sprint B')
        self.foreign_sprint = Sprint.objects.create(
            project=self.foreign_project, name='Foreign sprint')

        # Bulk assign resolves the identity through the user service before it
        # reaches the (possibly empty) authorized queryset.
        self.user_service_patch = patch('tasks.views.user_service')
        mocked_view_user_service = self.user_service_patch.start()
        self.addCleanup(self.user_service_patch.stop)
        # Item._resolve_user_ref imports the singleton from its defining module
        # at call time, so patch that reference as well.
        self.model_user_service_patch = patch('tasks.user_service.user_service')
        mocked_model_user_service = self.model_user_service_patch.start()
        self.addCleanup(self.model_user_service_patch.stop)
        users = {
            user.id: {'id': user.id, 'email': user.email}
            for user in (self.actor, self.owner, self.org_admin, self.staff,
                         self.nonstaff_superuser)
        }
        for mocked_user_service in (mocked_view_user_service, mocked_model_user_service):
            mocked_user_service.get_user_by_id.side_effect = (
                lambda user_id: users.get(int(user_id))
            )
            mocked_user_service.get_user_by_email.side_effect = (
                lambda email: next(
                    (record for record in users.values()
                     if record['email'].lower() == str(email).lower()),
                    None,
                )
            )

    def _bulk(self, user, operation, item_ids, data=None):
        self.client.force_authenticate(user)
        return self.client.post(
            '/api/items/bulk/',
            {
                'operation': operation,
                'item_ids': item_ids,
                'data': data or {},
            },
            format='json',
        )

    def _item(self, project, title, **kwargs):
        kwargs.setdefault('created_by_id', self.owner.id)
        return Item.objects.create(
            mode='task', project=project, title=title,
            status='pending', priority='normal',
            **kwargs,
        )

    def test_private_browse_only_member_cannot_run_any_bulk_mutation(self):
        cases = (
            ('transition', {'to_status': 'done'}, 'updated'),
            ('assign', {
                'assignee_id': self.actor.id,
                'assignee_email': self.actor.email,
            }, 'updated'),
            ('edit', {'priority': 'urgent'}, 'updated'),
            ('move_sprint', {'sprint_id': self.viewer_sprint.id}, 'updated'),
            ('delete', {}, 'deleted'),
        )

        for operation, data, response_key in cases:
            with self.subTest(operation=operation):
                item = self._item(self.viewer_project, f'viewer-{operation}')
                initial_status = item.status
                initial_priority = item.priority
                self.assertTrue(
                    PermissionService.get_user_item_queryset(self.actor)
                    .filter(pk=item.pk).exists(),
                    'fixture must be visible so this proves mutation, not visibility, gating',
                )

                response = self._bulk(self.actor, operation, [item.id], data)

                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.data[response_key], 0)
                self.assertTrue(Item.objects.filter(pk=item.pk).exists())
                item.refresh_from_db()
                self.assertEqual(item.status, initial_status)
                self.assertEqual(item.priority, initial_priority)
                self.assertIsNone(item.assignee_id)
                self.assertIsNone(item.sprint_id)

    def test_foreign_org_item_cannot_run_any_bulk_mutation(self):
        """Every operation shares the cross-tenant negative boundary."""
        cases = (
            ('transition', {'to_status': 'done'}, 'updated'),
            ('assign', {
                'assignee_id': self.actor.id,
                'assignee_email': self.actor.email,
            }, 'updated'),
            ('edit', {'priority': 'urgent'}, 'updated'),
            ('move_sprint', {'sprint_id': self.foreign_sprint.id}, 'updated'),
            ('delete', {}, 'deleted'),
        )

        for operation, data, response_key in cases:
            with self.subTest(operation=operation):
                item = self._item(self.foreign_project, f'foreign-{operation}')
                initial_status = item.status
                initial_priority = item.priority

                response = self._bulk(self.actor, operation, [item.id], data)

                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.data[response_key], 0)
                self.assertTrue(Item.objects.filter(pk=item.pk).exists())
                item.refresh_from_db()
                self.assertEqual(item.status, initial_status)
                self.assertEqual(item.priority, initial_priority)
                self.assertIsNone(item.assignee_id)
                self.assertIsNone(item.sprint_id)

    def test_open_org_member_can_bulk_edit_but_cannot_bulk_delete(self):
        edit_item = self._item(self.open_project, 'open-edit')
        delete_item = self._item(self.open_project, 'open-delete')

        response = self._bulk(
            self.actor, 'edit', [edit_item.id], {'priority': 'high'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data['updated'], 1)
        edit_item.refresh_from_db()
        self.assertEqual(edit_item.priority, 'high')

        response = self._bulk(self.actor, 'delete', [delete_item.id])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data['deleted'], 0)
        self.assertTrue(Item.objects.filter(pk=delete_item.pk).exists())

    def test_org_admin_staff_and_nonstaff_superuser_keep_bulk_authority(self):
        admin_item = self._item(self.viewer_project, 'admin-delete')
        response = self._bulk(self.org_admin, 'delete', [admin_item.id])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data['deleted'], 1)

        staff_item = self._item(self.foreign_project, 'staff-edit')
        response = self._bulk(
            self.staff, 'edit', [staff_item.id], {'priority': 'urgent'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data['updated'], 1)
        staff_item.refresh_from_db()
        self.assertEqual(staff_item.priority, 'urgent')

        root_item = self._item(self.foreign_project, 'root-delete')
        response = self._bulk(self.nonstaff_superuser, 'delete', [root_item.id])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data['deleted'], 1)

    def test_move_sprint_accepts_only_same_project_target(self):
        item = self._item(
            self.project_a, 'same-project-sprint', created_by_id=self.actor.id)

        response = self._bulk(
            self.actor, 'move_sprint', [item.id], {'sprint_id': self.sprint_a.id})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data['updated'], 1)
        item.refresh_from_db()
        self.assertEqual(item.sprint_id, self.sprint_a.id)

    def test_move_sprint_foreign_and_missing_targets_are_non_enumerable(self):
        item = self._item(
            self.project_a, 'foreign-sprint', created_by_id=self.actor.id)
        original_sprint_id = item.sprint_id

        for sprint_id in (self.sprint_b.id, self.foreign_sprint.id, 987654321):
            with self.subTest(sprint_id=sprint_id):
                response = self._bulk(
                    self.actor, 'move_sprint', [item.id], {'sprint_id': sprint_id})
                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(
                    response.data,
                    {'error': 'Invalid sprint for selected items'},
                )
                item.refresh_from_db()
                self.assertEqual(item.sprint_id, original_sprint_id)

    def test_move_sprint_mixed_authorized_projects_is_atomic(self):
        item_a = self._item(
            self.project_a, 'mixed-a', created_by_id=self.actor.id)
        item_b = self._item(
            self.project_b, 'mixed-b', created_by_id=self.actor.id)

        response = self._bulk(
            self.actor,
            'move_sprint',
            [item_a.id, item_b.id],
            {'sprint_id': self.sprint_a.id},
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.data, {'error': 'Invalid sprint for selected items'})
        item_a.refresh_from_db()
        item_b.refresh_from_db()
        self.assertIsNone(item_a.sprint_id)
        self.assertIsNone(item_b.sprint_id)

    def test_move_sprint_clear_remains_valid_across_projects(self):
        item_a = self._item(
            self.project_a, 'clear-a', created_by_id=self.actor.id,
            sprint=self.sprint_a,
        )
        item_b = self._item(
            self.project_b, 'clear-b', created_by_id=self.actor.id,
            sprint=self.sprint_b,
        )

        response = self._bulk(
            self.actor, 'move_sprint', [item_a.id, item_b.id], {'sprint_id': None})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data['updated'], 2)
        item_a.refresh_from_db()
        item_b.refresh_from_db()
        self.assertIsNone(item_a.sprint_id)
        self.assertIsNone(item_b.sprint_id)

    def test_unauthorized_empty_batch_cannot_probe_sprint_existence(self):
        foreign_item = self._item(self.foreign_project, 'foreign-secret')

        existing = self._bulk(
            self.actor,
            'move_sprint',
            [foreign_item.id],
            {'sprint_id': self.foreign_sprint.id},
        )
        missing = self._bulk(
            self.actor,
            'move_sprint',
            [foreign_item.id],
            {'sprint_id': 987654321},
        )

        self.assertEqual(existing.status_code, 200, existing.content)
        self.assertEqual(missing.status_code, 200, missing.content)
        self.assertEqual(existing.data, {'updated': 0})
        self.assertEqual(missing.data, existing.data)
        foreign_item.refresh_from_db()
        self.assertIsNone(foreign_item.sprint_id)
