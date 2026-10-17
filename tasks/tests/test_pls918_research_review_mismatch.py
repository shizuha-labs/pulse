"""PLS-918 regression fixture: recurring-item Submit-for-review destination
reservation is validated against the post-function target after the recurring
owner-team healer reverts the assignment, raising ``wip_reservation_target_mismatch``.

Production shape (BRW-1 gen 4):
  * recurring item with ``metadata.recurring_owner_team`` = source/owner lane
  * source lane roster NOT wip_enforced (so the healer cannot replace the token)
  * destination review lane roster wip_enforced (so assign_team MINTs a token)
  * transition: in_progress -> in_review with ``assign_team: <dest lane>``
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from tasks.models import (
    Item,
    Status,
    TeamRoster,
    WipSlotReservation,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService


class Pls918ResearchReviewMismatchTests(TransactionTestCase):
    OWNER_EMAIL = 'owner-seat@example.com'
    OWNER_UID = 91801
    DEST_EMAIL = 'review-seat@example.com'
    DEST_UID = 91802

    def _set_roster(self, team_name, email, uid, *, wip_enforced=False):
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name=team_name,
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[email]],
                'junior_emails': [email],
                'is_active': True,
                'wip_enforced': wip_enforced,
                'ready_wip_cap': 2,
                'active_wip_cap': 2,
            },
        )
        WorkflowService._invalidate_team_roster_caches()

    def _fixture(self):
        owner_email = self.OWNER_EMAIL
        owner_uid = self.OWNER_UID
        dest_email = self.DEST_EMAIL
        dest_uid = self.DEST_UID

        # user service resolving owner + destination seats
        class FakeUserService:
            def get_user_by_email(self, value):
                for uid, email in ((owner_uid, owner_email), (dest_uid, dest_email)):
                    if email == value:
                        return {'id': uid, 'email': email}
                return None

            def get_user_by_id(self, value):
                for uid, email in ((owner_uid, owner_email), (dest_uid, dest_email)):
                    if int(value) == uid:
                        return {'id': uid, 'email': email}
                return None

            def resolve_user_info(self, user_id=None, email=None):
                for uid, em in ((owner_uid, owner_email), (dest_uid, dest_email)):
                    if user_id is not None and int(user_id) == uid:
                        return {'id': uid, 'email': em, 'username': 'seat-%s' % uid,
                                'full_name': 'Seat %s' % uid, 'is_agent': True}
                    if email and email.strip().lower() == em and user_id is None:
                        return {'id': uid, 'email': em, 'username': 'seat-%s' % uid,
                                'full_name': 'Seat %s' % uid, 'is_agent': True}
                return None
        user_service = FakeUserService()

        actor = get_user_model().objects.create_user(
            username='pls918-owner-actor',
            email=owner_email,
            password='unused',
            is_staff=True,
        )

        self.project = None
        from tasks.models import Project
        project = Project.objects.create(name='PLS918', key='PLS918', org_id=1)

        # Owner lane exists but is NOT wip_enforced (config -> None on healer).
        self._set_roster('engineering', owner_email, owner_uid)
        # Destination research lane IS wip_enforced -> assign_team mint.
        self._set_roster('review', dest_email, dest_uid, wip_enforced=True)

        in_progress, _ = Status.objects.get_or_create(
            slug='in_progress',
            defaults={'name': 'In Progress', 'category': 'in_progress'},
        )
        in_review, _ = Status.objects.get_or_create(
            slug='in_review',
            defaults={'name': 'In Review', 'category': 'in_progress'},
        )
        workflow = Workflow.objects.create(
            name='PLS 918 research review',
            slug='pls-918-research-review',
            initial_status=in_progress,
            is_active=True,
        )
        for position, status in enumerate((in_progress, in_review)):
            WorkflowStatus.objects.create(
                workflow=workflow, status=status, position=position,
            )
        transition = WorkflowTransition.objects.create(
            workflow=workflow,
            name='Submit for Research Review',
            from_status='in_progress',
            to_status='in_review',
            position=0,
            post_functions=[{'type': 'assign_team', 'value': 'review'}],
        )

        item = Item.objects.create(
            title='BRW recurring research review',
            mode='task',
            status='in_progress',
            workflow_status='In Progress',
            priority='urgent',
            assignment_group='engineering',
            workflow_name=workflow.slug,
            project=project,
            is_recurring=True,
            frequency='weekly',
            labels={'no-pr': 'true'},
            metadata={'recurring_owner_team': 'engineering'},
        )
        Item.objects.filter(pk=item.pk).update(
            assignee_id=actor.id,
            assignee_email=actor.email,
            created_by_id=actor.id,
            created_by_email=actor.email,
        )
        item.refresh_from_db()
        return actor, item, transition, user_service

    def test_exact_recurring_sequence_start_progress_then_submit_research(self):
        """The exact BRW-1 reproduction: recurring generation reset to `open`,
        assigned owner -> Start Progress -> Submit for Research Review with a
        lane reassignment. Pre-fix, the submit rejected with
        ``wip_reservation_target_mismatch``; post-fix it reaches `in_review`
        on the review lane atomically with no dangling reservation."""
        from rest_framework.test import APIClient

        actor, item, start_progress, submit, user_service = self._fixture_full()
        client = APIClient()
        client.force_authenticate(user=actor)

        with patch('tasks.user_service.user_service', user_service), \
             patch('tasks.views.user_service', user_service), \
             patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 return_value=True,
             ):
            r1 = client.post(
                f'/api/items/{item.pk}/execute-transition/',
                {'transition_id': str(start_progress.id)},
                format='json',
            )
            item.refresh_from_db()
            self.assertEqual(r1.status_code, 200, r1.content)
            self.assertEqual(item.status, 'in_progress')

            r2 = client.post(
                f'/api/items/{item.pk}/execute-transition/',
                {'transition_id': str(submit.id)},
                format='json',
            )

        item.refresh_from_db()
        self.assertEqual(r2.status_code, 200, r2.content)
        self.assertEqual(item.status, 'in_review')
        self.assertEqual(item.assignment_group, 'review')
        self.assertFalse(WipSlotReservation.objects.exists())

    def _fixture_full(self):
        """Full-cycle fixture: open -> in_progress (Start Progress) -> in_review."""
        from tasks.models import Project

        owner_email = self.OWNER_EMAIL
        owner_uid = self.OWNER_UID
        dest_email = self.DEST_EMAIL
        dest_uid = self.DEST_UID

        class FakeUserService:
            def get_user_by_email(self, value):
                for uid, email in ((owner_uid, owner_email), (dest_uid, dest_email)):
                    if email == value:
                        return {'id': uid, 'email': email}
                return None

            def get_user_by_id(self, value):
                for uid, email in ((owner_uid, owner_email), (dest_uid, dest_email)):
                    if int(value) == uid:
                        return {'id': uid, 'email': email}
                return None

            def resolve_user_info(self, user_id=None, email=None):
                for uid, em in ((owner_uid, owner_email), (dest_uid, dest_email)):
                    if user_id is not None and int(user_id) == uid:
                        return {'id': uid, 'email': em, 'username': 'seat-%s' % uid,
                                'full_name': 'Seat %s' % uid, 'is_agent': True}
                    if email and email.strip().lower() == em and user_id is None:
                        return {'id': uid, 'email': em, 'username': 'seat-%s' % uid,
                                'full_name': 'Seat %s' % uid, 'is_agent': True}
                return None
        user_service = FakeUserService()

        actor = get_user_model().objects.create_user(
            username='pls918-owner-actor',
            email=owner_email,
            password='unused',
            is_staff=True,
        )
        project = Project.objects.create(name='PLS918', key='PLS918', org_id=1)
        self._set_roster('engineering', owner_email, owner_uid)
        self._set_roster('review', dest_email, dest_uid, wip_enforced=True)

        open_status, _ = Status.objects.get_or_create(
            slug='open', defaults={'name': 'Open', 'category': 'todo'},
        )
        in_progress, _ = Status.objects.get_or_create(
            slug='in_progress',
            defaults={'name': 'In Progress', 'category': 'in_progress'},
        )
        in_review, _ = Status.objects.get_or_create(
            slug='in_review',
            defaults={'name': 'In Review', 'category': 'in_progress'},
        )
        workflow = Workflow.objects.create(
            name='PLS 918 research review full',
            slug='pls-918-research-review-full',
            initial_status=open_status,
            is_active=True,
        )
        for position, status in enumerate((open_status, in_progress, in_review)):
            WorkflowStatus.objects.create(
                workflow=workflow, status=status, position=position,
            )
        start_progress = WorkflowTransition.objects.create(
            workflow=workflow,
            name='Start Progress',
            from_status='open',
            to_status='in_progress',
            position=0,
            post_functions=[],
        )
        submit = WorkflowTransition.objects.create(
            workflow=workflow,
            name='Submit for Research Review',
            from_status='in_progress',
            to_status='in_review',
            position=1,
            post_functions=[{'type': 'assign_team', 'value': 'review'}],
        )

        item = Item.objects.create(
            title='BRW recurring research review full',
            mode='task',
            status='open',
            workflow_status='Open',
            priority='urgent',
            assignment_group='engineering',
            workflow_name=workflow.slug,
            project=project,
            is_recurring=True,
            frequency='weekly',
            labels={'no-pr': 'true'},
            metadata={'recurring_owner_team': 'engineering'},
        )
        Item.objects.filter(pk=item.pk).update(
            assignee_id=actor.id,
            assignee_email=actor.email,
            created_by_id=actor.id,
            created_by_email=actor.email,
        )
        item.refresh_from_db()
        return actor, item, start_progress, submit, user_service

    def test_submit_for_research_review_on_recurring_item_reaches_in_review(self):
        from rest_framework.test import APIClient

        actor, item, transition, user_service = self._fixture()
        client = APIClient()
        client.force_authenticate(user=actor)

        with patch('tasks.user_service.user_service', user_service), \
             patch('tasks.views.user_service', user_service), \
             patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 return_value=True,
             ):
            response = client.post(
                f'/api/items/{item.pk}/execute-transition/',
                {'transition_id': str(transition.id)},
                format='json',
            )

        item.refresh_from_db()
        self.assertEqual(
            response.status_code, 200,
            f'expected 200, got {response.status_code}: {response.content}',
        )
        self.assertEqual(item.status, 'in_review')
        self.assertEqual(item.assignment_group, 'review')
        self.assertFalse(WipSlotReservation.objects.exists())
