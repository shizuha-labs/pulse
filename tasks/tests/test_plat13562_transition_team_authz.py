"""PLAT-13562: team-authority gate + out-of-team mutation detective.

Game-day gen13 probe 1b (PLAT-1238): a security-team seat's token moved a
devops-owned item todo→blocked with HTTP 200 — the transition route enforced
workflow validity but no ownership/team gate, and no detective signal fired.

Two legs:
1. Gate (carrier pulse#1024, merged): a seat outside the item's team gets
   403 on the transition route; the same-team path stays green.
2. Detective (this PR): a committed status change by an actor WITHOUT team
   authority raises a deduped anomaly alert — reachable only via non-route
   paths (service-level callers / bypasses), since the route refuses first.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from tasks.models import (
    Item, Project, ProjectMembership, Status, TeamRoster, Workflow,
    WorkflowStatus, WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService


def _status(slug, category='in_progress', position=1):
    obj, _ = Status.objects.get_or_create(
        slug=slug,
        defaults={'name': slug.replace('_', ' ').title(), 'category': category,
                  'position': position},
    )
    return obj


class _TransitionFixture(APITestCase):
    """org 1 with two rosters: team-a (actor A) and team-b (owns the item)."""

    @classmethod
    def setUpTestData(cls):
        cls.a_seat = get_user_model().objects.create_user(
            username='a-seat', email='a-seat@example.com', password='x',
        )
        cls.b_seat = get_user_model().objects.create_user(
            username='b-seat', email='b-seat@example.com', password='x',
        )
        cls.outsider = get_user_model().objects.create_user(
            username='outsider', email='outsider@example.com', password='x',
        )
        TeamRoster.objects.create(
            team_name='team-a', organization_id=1, tiers=[['a-seat@example.com']],
        )
        # The item's assignee sits on team-b's roster (Item.save validates
        # the assignee against the task org's target team roster).
        TeamRoster.objects.create(
            team_name='team-b', organization_id=1,
            tiers=[['b-seat@example.com', 'b-assignee@example.com']],
        )
        cls.project = Project.objects.create(
            name='T13562', key='T356', org_id=1, access_level='open')
        # Visibility for all three seats (the probe had project visibility —
        # visibility is NOT team authority, that is the finding).
        for user in (cls.a_seat, cls.b_seat, cls.outsider):
            ProjectMembership.objects.create(
                project=cls.project, user_id=user.id,
                user_email=user.email, role='member', is_active=True,
            )
        cls.open_status = _status('todo', category='open', position=1)
        cls.blocked_status = _status('blocked', category='open', position=2)
        cls.wip_status = _status('in_progress', category='in_progress', position=3)
        cls.workflow = Workflow.objects.create(
            name='T13562 WF', slug='t13562-wf', initial_status=cls.open_status)
        WorkflowStatus.objects.create(workflow=cls.workflow, status=cls.open_status, position=1)
        WorkflowStatus.objects.create(workflow=cls.workflow, status=cls.blocked_status, position=2)
        WorkflowStatus.objects.create(workflow=cls.workflow, status=cls.wip_status, position=3)
        # A naked block is refused by design (owned-blocker gate), so the
        # exercised transition is the ordinary Start Work hop.
        cls.transition = WorkflowTransition.objects.create(
            workflow=cls.workflow, name='Start Work',
            from_status='todo', to_status='in_progress')

    def setUp(self):
        self.client = APIClient()
        # The visibility resolver prefers the JWT-shaped claim; the probe
        # seat's visibility came from real org membership in org 1.
        for user in (self.a_seat, self.b_seat, self.outsider):
            user.organization_memberships = {1: 'member'}
        self.item = Item.objects.create(
            mode='task', title='devops-owned card', project=self.project,
            item_key='T356-1', status='todo',
            workflow_name=self.workflow.name,
            assignee_email='b-assignee@example.com',  # on team-b's roster
            assignment_group='team-b',          # devops-team-owned
        )

    def _post_transition(self, user):
        self.client.force_authenticate(user=user)
        return self.client.post(
            f'/api/items/{self.item.item_key}/execute-transition/',
            {'to_status': 'in_progress', 'transition_id': str(self.transition.id)},
            format='json',
        )


class TransitionTeamGateTests(_TransitionFixture):
    def test_cross_team_seat_is_refused_403(self):
        """Negative polarity (PLAT-1236 pattern): team-a seat on team-b's item."""
        response = self._post_transition(self.a_seat)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'todo', 'state must not change')

    def test_project_visibility_alone_is_not_authority(self):
        """The gen13 probe's exact shape: a member of NO team, with visibility."""
        response = self._post_transition(self.outsider)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_same_team_seat_is_green(self):
        response = self._post_transition(self.b_seat)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'in_progress')

    def test_assignee_is_green(self):
        assignee = get_user_model().objects.create_user(
            username='b-assignee', email='b-assignee@example.com', password='x')
        ProjectMembership.objects.create(
            project=self.project, user_id=assignee.id,
            user_email=assignee.email, role='member', is_active=True)
        response = self._post_transition(assignee)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class OutOfTeamMutationDetectiveTests(_TransitionFixture):
    """Leg 2: committed mutations by non-authority actors raise the alert."""

    def _detective_alerts(self):
        from tasks.models import Item as ItemModel
        return ItemModel.objects.filter(
            mode='alert', source='pulse-workflow-authz',
        )

    def test_service_level_cross_team_transition_raises_alert(self):
        """The route refuses cross-team seats (403 above); a DIRECT engine
        call is the bypass face the detective must catch."""
        with mock.patch('tasks.tasks.send_alert_notification.delay'):
            WorkflowService.execute_transition(
                self.item, 'in_progress', user=self.a_seat,
            )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'in_progress', 'mutation committed (fail-open detection)')
        alerts = self._detective_alerts()
        self.assertTrue(alerts.exists(), 'out-of-team mutation must raise the anomaly alert')
        alert = alerts.first()
        self.assertEqual(alert.priority, 'high')
        self.assertEqual(alert.assignment_group, 'security')
        self.assertIn('a-seat@example.com', alert.description)
        self.assertEqual(alert.payload.get('old_status'), 'todo')
        self.assertEqual(alert.payload.get('new_status'), 'in_progress')

    def test_same_team_service_transition_raises_no_alert(self):
        with mock.patch('tasks.tasks.send_alert_notification.delay'):
            WorkflowService.execute_transition(
                self.item, 'in_progress', user=self.b_seat,
            )
        self.assertFalse(self._detective_alerts().exists())

    def test_system_actor_raises_no_alert(self):
        from tasks.services.team_authz import maybe_alert_out_of_team_transition
        system_user = mock.Mock(id=0, email='pr-sync@shizuha.local', is_staff=False)
        self.assertIsNone(
            maybe_alert_out_of_team_transition(self.item, system_user, 'todo'))

    def test_staff_override_raises_no_alert(self):
        """The operator path is by design (the gate allows staff); alerting on
        it would bury the anomaly signal under legitimate operator actions."""
        staff = get_user_model().objects.create_user(
            username='op', email='op@example.com', password='x', is_staff=True)
        with mock.patch('tasks.tasks.send_alert_notification.delay'):
            WorkflowService.execute_transition(self.item, 'in_progress', user=staff)
        self.assertFalse(self._detective_alerts().exists())

    def test_alert_dedupes_per_item_and_actor(self):
        with mock.patch('tasks.tasks.send_alert_notification.delay'):
            WorkflowService.execute_transition(self.item, 'in_progress', user=self.a_seat)
            # Reopen via a same-team seat and repeat the bypass.
            self.item.status = 'todo'
            self.item.save(update_fields=['status'])
            WorkflowService.execute_transition(self.item, 'in_progress', user=self.a_seat)
        alerts = self._detective_alerts()
        self.assertEqual(
            alerts.count(), 1,
            'AlertDeduplicationService must collapse replays of the same anomaly')

    def test_detective_failure_never_breaks_the_transition(self):
        """Fail-open by contract: an exploding alert path must not roll back
        the committed state change."""
        from tasks.services.deduplication import AlertDeduplicationService
        with mock.patch.object(
                AlertDeduplicationService, 'submit_alert',
                side_effect=RuntimeError('dedupe down')), \
             mock.patch('tasks.tasks.send_alert_notification.delay'):
            WorkflowService.execute_transition(self.item, 'in_progress', user=self.a_seat)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'in_progress')
