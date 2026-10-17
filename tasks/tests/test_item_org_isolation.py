from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from tasks.models import Item, ItemLink, Project, ProjectMembership, TeamRoster


class ItemOrganizationIsolationTests(TestCase):
    def setUp(self):
        self.org_one = Project.objects.create(org_id=1, name='One', key='ONE')
        self.org_one_other = Project.objects.create(
            org_id=1,
            name='One Other',
            key='ONO',
        )
        self.org_two = Project.objects.create(org_id=2, name='Two', key='TWO')
        self.one_a = Item.objects.create(project=self.org_one, title='One A')
        self.one_b = Item.objects.create(project=self.org_one, title='One B')
        self.one_other = Item.objects.create(
            project=self.org_one_other,
            title='One Other',
        )
        self.two = Item.objects.create(project=self.org_two, title='Two')

    def test_same_org_link_is_allowed(self):
        link = ItemLink.objects.create(
            source_item=self.one_a,
            target_item=self.one_b,
            link_type='blocks',
            created_by_id=1,
            created_by_email='owner@example.com',
        )
        self.assertIsNotNone(link.pk)

    def test_cross_org_link_is_rejected_for_every_writer(self):
        with self.assertRaisesMessage(
            ValidationError,
            'Linked tasks must belong to the same organization.',
        ):
            ItemLink.objects.create(
                source_item=self.one_a,
                target_item=self.two,
                link_type='relates_to',
                created_by_id=1,
                created_by_email='owner@example.com',
            )

    def test_org_owned_item_cannot_link_to_unscoped_item(self):
        unscoped = Item.objects.create(title='Legacy personal item')
        with self.assertRaises(ValidationError):
            ItemLink.objects.create(
                source_item=self.one_a,
                target_item=unscoped,
                link_type='relates_to',
                created_by_id=1,
                created_by_email='owner@example.com',
            )

    def test_two_unscoped_legacy_items_may_link(self):
        source = Item.objects.create(title='Legacy A')
        target = Item.objects.create(title='Legacy B')
        link = ItemLink.objects.create(
            source_item=source,
            target_item=target,
            link_type='relates_to',
            created_by_id=1,
            created_by_email='owner@example.com',
        )
        self.assertIsNotNone(link.pk)

    def test_cross_org_parent_is_rejected(self):
        self.two.parent_item = self.one_a
        with self.assertRaisesMessage(
            ValidationError,
            'Parent and child tasks must belong to the same organization.',
        ):
            self.two.save()

    def test_same_org_parent_is_allowed(self):
        self.one_other.parent_item = self.one_a
        self.one_other.save()
        self.assertEqual(self.one_other.parent_item_id, self.one_a.id)

    def test_cross_org_parent_alert_is_rejected(self):
        self.two.parent_alert = self.one_a
        with self.assertRaisesMessage(
            ValidationError,
            'Parent and child tasks must belong to the same organization.',
        ):
            self.two.save()

    def test_task_cannot_move_to_project_in_another_organization(self):
        self.one_a.project = self.org_two
        with self.assertRaisesMessage(
            ValidationError,
            'A task cannot be moved to another organization.',
        ):
            self.one_a.save()

    def test_task_can_move_between_projects_in_same_organization(self):
        self.one_a.project = self.org_one_other
        self.one_a.save()
        self.assertEqual(self.one_a.project_id, self.org_one_other.id)

    @mock.patch(
        'tasks.services.admin_service.admin_service.get_organization',
        return_value={'id': 1, 'slug': 'org-one', 'name': 'Org One'},
    )
    def test_direct_writer_cannot_assign_foreign_team_member(self, _get_org):
        TeamRoster.objects.create(
            organization_id=1,
            organization_slug='org-one',
            team_name='accounting',
            tiers=[['member@org-one.example']],
        )

        with self.assertRaisesMessage(
            ValidationError,
            'Assignee is not authorized for this task organization Team',
        ):
            Item.objects.create(
                project=self.org_one,
                title='Invalid assignment',
                assignment_group='accounting',
                assignee_id=99,
                assignee_email='foreign@org-two.example',
            )

    @mock.patch(
        'tasks.services.admin_service.admin_service.get_organization',
        return_value={'id': 1, 'slug': 'org-one', 'name': 'Org One'},
    )
    def test_direct_writer_can_assign_exact_org_team_member(self, _get_org):
        TeamRoster.objects.create(
            organization_id=1,
            organization_slug='org-one',
            team_name='accounting',
            tiers=[['member@org-one.example']],
        )

        item = Item.objects.create(
            project=self.org_one,
            title='Valid assignment',
            assignment_group='accounting',
            assignee_id=98,
            assignee_email='member@org-one.example',
        )

        self.assertEqual(item.assignee_email, 'member@org-one.example')

    def test_same_agent_team_memberships_are_independent_across_orgs(self):
        email = 'shared-runtime@shizuha.com'
        org_one_roster = TeamRoster.objects.create(
            organization_id=1,
            organization_slug='org-one',
            team_name='org-one-specialists',
            tiers=[[email]],
        )
        TeamRoster.objects.create(
            organization_id=2,
            organization_slug='org-two',
            team_name='org-two-specialists',
            tiers=[[email]],
        )

        org_one_roster.refresh_from_db()
        self.assertEqual(org_one_roster.tiers, [[email]])

    @mock.patch(
        'tasks.services.admin_service.admin_service.get_org_members',
        return_value=[],
    )
    @mock.patch(
        'tasks.services.admin_service.admin_service.get_organization',
        return_value={'id': 1, 'slug': 'shizuha', 'name': 'Shizuha'},
    )
    def test_direct_foreign_assignee_without_team_is_rejected(
        self,
        _get_org,
        _get_members,
    ):
        with self.assertRaisesMessage(
            ValidationError,
            'Assignee is not authorized for this task organization Team',
        ):
            Item.objects.create(
                project=self.org_one,
                title='Foreign direct assignment',
                assignee_email='foreign@example.net',
            )


class ItemOrganizationIsolationDatabaseTests(TransactionTestCase):
    """PostgreSQL triggers cover bulk/query writers that bypass model.save()."""

    reset_sequences = True

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Database trigger coverage is PostgreSQL-specific.')
        self.org_one = Project.objects.create(org_id=1, name='One', key='ONE')
        self.org_two = Project.objects.create(org_id=2, name='Two', key='TWO')
        self.one = Item.objects.create(
            project=self.org_one,
            title='One',
            workflow_name='simple',
            status='todo',
        )
        self.two = Item.objects.create(
            project=self.org_two,
            title='Two',
            workflow_name='simple',
            status='todo',
        )

    def test_bulk_link_writer_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ItemLink.objects.bulk_create([
                ItemLink(
                    source_item=self.one,
                    target_item=self.two,
                    link_type='relates_to',
                    created_by_id=1,
                    created_by_email='owner@example.com',
                )
            ])

    def test_queryset_parent_writer_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Item.objects.filter(pk=self.two.pk).update(parent_item=self.one)

    def test_project_organization_change_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Project.objects.filter(pk=self.org_one.pk).update(org_id=2)

    def test_queryset_task_move_to_another_org_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Item.objects.filter(pk=self.one.pk).update(project=self.org_two)

    def test_queryset_foreign_team_assignment_is_rejected_by_database(self):
        TeamRoster.objects.create(
            organization_id=1,
            organization_slug='shizuha',
            team_name='accounting',
            tiers=[['member@shizuha.com']],
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Item.objects.filter(pk=self.one.pk).update(
                assignment_group='accounting',
                assignee_id=99,
                assignee_email='foreign@example.net',
            )

    def test_noop_model_save_of_authorized_direct_assignment_is_allowed(self):
        ProjectMembership.objects.create(
            project=self.org_one,
            user_id=17,
            user_email='member@example.net',
            role='member',
        )
        with mock.patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            item = Item.objects.create(
                project=self.org_one,
                title='Direct assignment',
                status='todo',
                workflow_name='simple',
                assignee_id=17,
                assignee_email='member@example.net',
            )
            item.save()
        item.refresh_from_db()

        self.assertEqual(item.assignee_email, 'member@example.net')
