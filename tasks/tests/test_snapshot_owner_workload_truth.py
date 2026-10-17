from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from tasks.models import Item, ItemLink, Project, ProjectMembership, Status, Workflow, WorkflowStatus
from tasks.services.actionable_items import annotate_owner_queue_projection
from tasks.views import OrgChartSnapshotView


class SnapshotOwnerWorkloadTruthTests(APITestCase):
    team = 'research-analytics'

    def setUp(self):
        self.owner = get_user_model().objects.create_user(
            username='snapshot-owner', email='snapshot-owner@shizuha.com', password='x',
            is_staff=True, is_superuser=True,
        )
        self.client.force_authenticate(self.owner)
        self.project = Project.objects.create(name='Owner workload', key='WLA', org_id=1, access_level='open')
        self.other_project = Project.objects.create(name='Other workload', key='WLB', org_id=2, access_level='open')
        for project in (self.project, self.other_project):
            ProjectMembership.objects.create(
                project=project, user_id=self.owner.pk, user_email=self.owner.email, is_active=True,
            )
        authorization = patch(
            'tasks.services.workflow_service.WorkflowService.assignment_org_mismatch',
            return_value=(False, 'fixture-org', 'fixture-org'),
        )
        authorization.start()
        self.addCleanup(authorization.stop)

    def item(self, *, project=None, status='resolved_elsewhere', workflow='simple', assigned=True, **fields):
        item = Item.objects.create(
            title='Workload projection fixture', mode='task', project=project or self.project,
            workflow_name='simple', status='todo', assignment_group=self.team,
            assignee_id=self.owner.pk if assigned else None,
            assignee_email=self.owner.email if assigned else None,
            created_by_id=self.owner.pk, created_by_email=self.owner.email,
        )
        Item.objects.filter(pk=item.pk).update(status=status, workflow_name=workflow, **fields)
        item.refresh_from_db()
        return item

    def snapshot(self, org_scope='all'):
        with patch('tasks.services.admin_service.admin_service.get_organization_by_slug', return_value={'id': 1}):
            return OrgChartSnapshotView()._aggregate_workload(org_scope=org_scope)

    def assert_buckets(self, counts, **expected):
        for bucket in ('open', 'in_progress', 'blocked', 'in_review', 'scheduled', 'backlog'):
            self.assertEqual(counts.get(bucket, 0), expected.get(bucket, 0), (bucket, counts))
        self.assertTrue(all(value >= 0 for value in counts.values()), counts)

    def test_real_preflight_and_snapshot_agree_for_live_rui_row_shapes(self):
        awareness = self.item()
        archived_review = self.item(project=self.other_project, status='in_review', workflow='autonomous-dev')
        Project.objects.filter(pk=archived_review.project_id).update(is_archived=True)
        self.item(status='todo', is_recurring=True, due_date=timezone.now() + timedelta(days=3), is_archived=True)
        self.item(status='todo', is_recurring=True, due_date=timezone.now() + timedelta(days=2))

        response = self.client.get('/api/items/heartbeat-preflight/', {'assignee_email': self.owner.email})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data['decision'], 'skip', response.data)
        self.assertEqual(response.data['ready_task_count'], 0, response.data)
        projected = annotate_owner_queue_projection(Item.objects.filter(pk=awareness.pk)).get()
        self.assertTrue(projected._queue_owner_awareness)
        self.assertTrue(projected._queue_is_waiting)
        by_email, _, _, _, _ = self.snapshot()
        self.assert_buckets(by_email[self.owner.email], blocked=1, scheduled=1)

    def test_generic_awareness_moves_once_in_all_five_maps_and_org_scope(self):
        self.item()
        self.item(assigned=False)
        self.item(project=self.other_project)
        by_email, by_group, by_uid, by_team, unassigned = self.snapshot()
        self.assert_buckets(by_email[self.owner.email], blocked=2)
        self.assert_buckets(by_uid[str(self.owner.pk)], blocked=2)
        self.assert_buckets(by_team[f'{self.owner.email}::{self.team}'], blocked=2)
        self.assert_buckets(by_team[f'{self.owner.pk}::{self.team}'], blocked=2)
        self.assert_buckets(by_group[self.team], blocked=3)
        self.assert_buckets(by_group[f'org1::{self.team}'], blocked=2)
        self.assert_buckets(by_group[f'org2::{self.team}'], blocked=1)
        self.assert_buckets(unassigned[self.team], blocked=1)
        self.assert_buckets(unassigned[f'org1::{self.team}'], blocked=1)
        scoped = self.snapshot('shizuha')
        self.assert_buckets(scoped[0][self.owner.email], blocked=1)
        self.assert_buckets(scoped[1][self.team], blocked=2)
        self.assertNotIn(f'org2::{self.team}', scoped[1])

    def test_policy_is_workflow_specific_and_supports_slug_or_name_case(self):
        status = Status.objects.get(slug='resolved_elsewhere')
        custom = Workflow.objects.create(name='Owner Status Is Actionable', slug='owner-status-actionable', initial_status=status)
        WorkflowStatus.objects.create(workflow=custom, status=status, owner_action_policy='actionable')
        simple = Workflow.objects.get(slug='simple')
        self.item(workflow=simple.name.upper())
        self.item(workflow=custom.slug.upper())
        self.assert_buckets(self.snapshot()[0][self.owner.email], open=1, blocked=1)

    def test_policy_and_status_changes_rearm_without_persisted_projection(self):
        item = self.item()
        membership = WorkflowStatus.objects.get(workflow__slug='simple', status__slug='resolved_elsewhere')
        self.assert_buckets(self.snapshot()[0][self.owner.email], blocked=1)
        WorkflowStatus.objects.filter(pk=membership.pk).update(owner_action_policy='actionable')
        self.assert_buckets(self.snapshot()[0][self.owner.email], open=1)
        WorkflowStatus.objects.filter(pk=membership.pk).update(owner_action_policy='awareness')
        self.assert_buckets(self.snapshot()[0][self.owner.email], blocked=1)
        Item.objects.filter(pk=item.pk).update(status='todo')
        self.assert_buckets(self.snapshot()[0][self.owner.email], open=1)
        Item.objects.filter(pk=item.pk).update(status='resolved_elsewhere')
        self.assert_buckets(self.snapshot()[0][self.owner.email], blocked=1)

    def test_item_and_project_archive_rearm_remove_every_active_map(self):
        item = self.item(status='in_progress')
        unassigned = self.item(status='todo', assigned=False)
        self.assert_buckets(self.snapshot()[1][self.team], open=1, in_progress=1)
        Item.objects.filter(pk=item.pk).update(is_archived=True)
        self.assertNotIn(self.owner.email, self.snapshot()[0])
        self.assertNotIn(str(self.owner.pk), self.snapshot()[2])
        Item.objects.filter(pk=item.pk).update(is_archived=False)
        self.assert_buckets(self.snapshot()[0][self.owner.email], in_progress=1)
        Project.objects.filter(pk=self.project.pk).update(is_archived=True)
        self.assertEqual(self.snapshot(), ({}, {}, {}, {}, {}))
        Project.objects.filter(pk=self.project.pk).update(is_archived=False)
        self.assert_buckets(self.snapshot()[1][self.team], open=1, in_progress=1)
        Item.objects.filter(pk=unassigned.pk).update(is_archived=True)
        self.assertEqual(self.snapshot()[4], {})

    def test_archived_overdue_is_removed_but_archived_completed_history_remains(self):
        self.item(status='in_progress', due_date=timezone.now() - timedelta(days=2), is_archived=True)
        self.item(status='done', resolution_date=timezone.now(), is_archived=True)
        Project.objects.filter(pk=self.project.pk).update(is_archived=True)
        by_email, by_group, by_uid, by_team, _ = self.snapshot()
        for counts in (by_email[self.owner.email], by_group[self.team], by_uid[str(self.owner.pk)],
                       by_team[f'{self.owner.email}::{self.team}'], by_team[f'{self.owner.pk}::{self.team}']):
            self.assert_buckets(counts)
            self.assertEqual(counts['completed'], 1)
            self.assertEqual(counts['overdue'], 0)

    def test_rfc_overlap_is_counted_once_and_each_reason_rearms_independently(self):
        membership = WorkflowStatus.objects.get(workflow__slug='feature-request-rfc', status__slug='rfc_implementation_linked')
        WorkflowStatus.objects.filter(pk=membership.pk).update(owner_action_policy='awareness')
        carrier = self.item(status='rfc_implementation_linked', workflow='feature-request-rfc')
        followup = self.item(status='todo', assigned=False)
        ItemLink.objects.create(source_item=carrier, target_item=followup, link_type='implemented_by', created_by_id=self.owner.pk)
        self.assert_buckets(self.snapshot()[0][self.owner.email], blocked=1)
        Item.objects.filter(pk=followup.pk).update(status='done')
        self.assert_buckets(self.snapshot()[0][self.owner.email], blocked=1)
        WorkflowStatus.objects.filter(pk=membership.pk).update(owner_action_policy='actionable')
        self.assert_buckets(self.snapshot()[0][self.owner.email], in_progress=1)
        Item.objects.filter(pk=followup.pk).update(status='todo')
        self.assert_buckets(self.snapshot()[0][self.owner.email], blocked=1)
        Item.objects.filter(pk=followup.pk).update(status='done')
        self.assert_buckets(self.snapshot()[0][self.owner.email], in_progress=1)
