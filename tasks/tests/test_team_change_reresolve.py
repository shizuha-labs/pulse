"""#9532 regression: PATCHing assignment_group must re-resolve the assignee.

The PATCH path (behind pulse_update_task) wrote a new team while silently
keeping the old assignee — a non-member (the operator on #9532) stayed on the
task, stranding it on their personal queue. Team routing is authoritative:
non-member assignee → re-resolve to a team member (or group-queue when full);
member assignee → keep (continuity).
"""
from unittest.mock import patch

from tasks.models import Item, Project, TeamRoster
from tasks.tests.base import AuthenticatedAPITestCase


class TeamChangeReresolveTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Team change test',
            key='TCR',
            org_id=1,
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='architecture',
            defaults={
                'organization_id': 1,
                'tiers': [['aoi@shizuha.com', 'nova@shizuha.com']],
            },
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='engineering',
            defaults={
                'organization_id': 1,
                'tiers': [['operator@example.com']],
            },
        )

    def _item(self, **kw):
        d = dict(project=self.project,
                 title='team-change target', mode='task', item_type='task.generic',
                 status='open', assignment_group='engineering',
                 assignee_email='operator@example.com')
        d.update(kw)
        return Item.objects.create(**d)

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
           return_value=(None, 'aoi@shizuha.com'))
    @patch('tasks.services.workflow_service.WorkflowService._active_team_candidate_emails',
           return_value=['aoi@shizuha.com', 'nova@shizuha.com'])
    def test_group_change_reresolves_nonmember_assignee(self, _m1, _m2):
        it = self._item()
        r = self.client.patch(f'/api/items/{it.id}/',
                              {'assignment_group': 'architecture'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        it.refresh_from_db()
        self.assertEqual(it.assignment_group, 'architecture')
        self.assertEqual(it.assignee_email, 'aoi@shizuha.com')

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
           return_value=(None, None))
    @patch('tasks.services.workflow_service.WorkflowService._active_team_candidate_emails',
           return_value=['aoi@shizuha.com'])
    def test_group_change_team_full_clears_to_group_queue(self, _m1, _m2):
        it = self._item()
        r = self.client.patch(f'/api/items/{it.id}/',
                              {'assignment_group': 'architecture'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        it.refresh_from_db()
        self.assertIsNone(it.assignee_id)
        self.assertFalse(it.assignee_email)

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee')
    @patch('tasks.services.workflow_service.WorkflowService._active_team_candidate_emails',
           return_value=['operator@example.com', 'aoi@shizuha.com'])
    def test_member_assignee_kept_on_group_change(self, _m1, resolver):
        # current assignee IS a member of the new team → continuity, no churn
        it = self._item()
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='architecture',
            defaults={
                'organization_id': 1,
                'tiers': [['operator@example.com', 'aoi@shizuha.com']],
            },
        )
        r = self.client.patch(f'/api/items/{it.id}/',
                              {'assignment_group': 'architecture'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        it.refresh_from_db()
        self.assertEqual(it.assignee_email, 'operator@example.com')
        resolver.assert_not_called()

    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee')
    def test_unchanged_group_does_not_reresolve(self, resolver):
        it = self._item()
        r = self.client.patch(f'/api/items/{it.id}/',
                              {'assignment_group': 'engineering'}, format='json')
        self.assertEqual(r.status_code, 200, r.content)
        it.refresh_from_db()
        self.assertEqual(it.assignee_email, 'operator@example.com')
        resolver.assert_not_called()
