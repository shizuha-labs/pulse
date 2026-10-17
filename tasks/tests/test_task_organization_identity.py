from unittest.mock import patch

from django.test import TestCase

from tasks.models import Item, Project
from tasks.serializers import (
    ItemCreateSerializer,
    ItemListSerializer,
    ItemSerializer,
    ProjectCreateSerializer,
    ProjectSerializer,
)


class TaskOrganizationIdentityTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            org_id=4,
            name='Books',
            key='BKSH',
        )
        self.item = Item.objects.create(
            project=self.project,
            item_key='BKSH-1',
            title='Prepare source pack',
            mode='task',
            assignment_group='accounting',
        )

    @patch.object(
        Project,
        'get_organization',
        return_value={'id': 4, 'slug': 'personal-3', 'name': "Hritik's Workspace"},
    )
    def test_detail_exposes_one_explicit_owning_organization(self, get_organization):
        data = ItemSerializer(self.item).data

        self.assertEqual(data['organization_id'], 4)
        self.assertEqual(data['organization_slug'], 'personal-3')
        self.assertEqual(data['organization_name'], "Hritik's Workspace")
        self.assertEqual(data['assignment_team']['canonical_key'], 'personal-3:accounting')
        self.assertEqual(data['assignment_team']['canonical_name'], 'accounting')
        self.assertEqual(data['assignment_team']['organization_id'], 4)
        self.assertEqual(get_organization.call_count, 1)

    def test_list_exposes_stable_org_id_without_admin_enrichment(self):
        with patch.object(Project, 'get_organization') as get_organization:
            data = ItemListSerializer(self.item).data

        self.assertEqual(data['organization_id'], 4)
        get_organization.assert_not_called()

    def test_project_cannot_be_moved_to_another_organization(self):
        serializer = ProjectSerializer(
            self.project,
            data={'org_id': 2},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('org_id', serializer.errors)

    def test_project_key_is_reserved_within_organization(self):
        serializer = ProjectCreateSerializer(data={
            'org_id': self.project.org_id,
            'name': 'Another Books',
            'key': self.project.key,
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn('key', serializer.errors)

    def test_project_key_can_be_reused_by_another_organization(self):
        serializer = ProjectCreateSerializer(data={
            'org_id': 2,
            'name': 'Another Books',
            'key': self.project.key,
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_task_cannot_be_moved_to_another_organization(self):
        other_project = Project.objects.create(
            org_id=2,
            name='Other Books',
            key='OBK',
        )
        serializer = ItemSerializer(
            self.item,
            data={'project': other_project.id},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('project', serializer.errors)

    def test_new_task_requires_an_organization_project(self):
        serializer = ItemCreateSerializer(data={
            'mode': 'task',
            'title': 'Unscoped task',
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn('project', serializer.errors)

    def test_same_project_key_across_orgs_still_mints_unique_task_keys(self):
        other_project = Project.objects.create(
            org_id=2,
            name='Other Books',
            key=self.project.key,
        )

        first_key = self.project.get_next_item_key()
        Item.objects.create(
            project=self.project,
            item_key=first_key,
            title='First',
        )
        second_key = other_project.get_next_item_key()

        self.assertNotEqual(first_key, second_key)
        self.assertTrue(first_key.startswith(f'{self.project.key}-'))
        self.assertTrue(second_key.startswith(f'{other_project.key}-'))
