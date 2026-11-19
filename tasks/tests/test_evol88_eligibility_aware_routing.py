"""EVOL-88: eligibility_aware resolver flag for routing instruments."""
import os
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Comment, Item, ItemActivity, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService


class _FakeUserService:
    users = {
        'eligible@shizuha.com': {'id': 201, 'email': 'eligible@shizuha.com'},
        'submitter@shizuha.com': {'id': 202, 'email': 'submitter@shizuha.com'},
        'ineligible@shizuha.com': {'id': 203, 'email': 'ineligible@shizuha.com'},
    }

    def get_user_by_email(self, email):
        return self.users.get((email or '').strip().lower())


def _can_execute(email, item, user_id=None):
    return (email or '').strip().lower() != 'ineligible@shizuha.com'


class Evol88EligibilityAwareRoutingTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Pulse', key='EVOL88', org_id=1, access_level='open',
        )
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={
                'tiers': [[
                    'eligible@shizuha.com',
                    'submitter@shizuha.com',
                    'ineligible@shizuha.com',
                ]],
                'wip_enforced': False,
            },
        )
        WorkflowService._invalidate_team_roster_caches()

    def tearDown(self):
        WorkflowService._invalidate_team_roster_caches()

    def _item(self, **kwargs):
        data = dict(
            project=self.project,
            title='evol-88 routing',
            mode='task',
            item_type='task.bug',
            workflow_name='autonomous-bug',
            status='in_review',
            workflow_status='in_review',
            assignment_group='engineering',
            created_by_id=202,
            created_by_email='submitter@shizuha.com',
        )
        data.update(kwargs)
        return Item.objects.create(**data)

    def _submit(self, item, user_id=202, email='submitter@shizuha.com'):
        ItemActivity.objects.create(
            item=item,
            action='status_changed',
            field_name='Status',
            old_value='in_progress',
            new_value=item.status,
            user_id=user_id,
            user_email=email,
        )

    @patch.dict(os.environ, {'PULSE_ROUTABILITY_ENFORCE': '1'})
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', staticmethod(_can_execute))
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._agent_workable_load', return_value=0)
    @patch('tasks.services.workflow_service.WorkflowService._agent_active_load', return_value=0)
    def test_eligible_seat_wins_over_submitter_and_role_ineligible(self, *_mocks):
        item = self._item()
        self._submit(item)
        uid, email = WorkflowService._resolve_team_assignee(
            'engineering', item=item, enforce_cap=False, eligibility_aware=True,
        )
        self.assertEqual((uid, email), (201, 'eligible@shizuha.com'))

    @patch.dict(os.environ, {'PULSE_ROUTABILITY_ENFORCE': '1'})
    @patch('tasks.services.workflow_service.WorkflowService._notify_routability_hold', return_value=True)
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', staticmethod(_can_execute))
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_pool_of_only_ineligible_emits_no_eligible_seat(self, *_mocks):
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={
                'tiers': [['submitter@shizuha.com', 'ineligible@shizuha.com']],
                'wip_enforced': False,
            },
        )
        WorkflowService._invalidate_team_roster_caches()
        item = self._item()
        self._submit(item)
        uid, email = WorkflowService._resolve_team_assignee(
            'engineering', item=item, enforce_cap=False, eligibility_aware=True,
        )
        self.assertEqual((uid, email), (None, None))
        hold = Comment.objects.filter(
            item=item, content__contains='PLS-278 routability hold',
        ).first()
        self.assertIsNotNone(hold)
        self.assertIn('no eligible seat', hold.content)

    @patch.dict(os.environ, {'PULSE_ROUTABILITY_ENFORCE': '1'})
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', staticmethod(_can_execute))
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._agent_workable_load', return_value=0)
    @patch('tasks.services.workflow_service.WorkflowService._agent_active_load', return_value=0)
    def test_flag_off_keeps_legacy_assignment(self, *_mocks):
        item = self._item()
        self._submit(item)
        uid, email = WorkflowService._resolve_team_assignee(
            'engineering', item=item, enforce_cap=False, eligibility_aware=False,
        )
        # Least-loaded among the roster; ineligible is not refused when the flag is off.
        self.assertIsNotNone(email)
        self.assertIn(email, {
            'eligible@shizuha.com',
            'submitter@shizuha.com',
            'ineligible@shizuha.com',
        })

    def test_lowercase_status_field_name_is_the_submitter(self):
        item = self._item()
        ItemActivity.objects.create(
            item=item,
            action='status_changed',
            field_name='status',
            old_value='in_progress',
            new_value=item.status,
            user_id=201,
            user_email='eligible@shizuha.com',
        )
        sub_id, sub_email = WorkflowService._submitter_of_record(item)
        self.assertEqual(sub_id, 201)
        self.assertEqual(sub_email, 'eligible@shizuha.com')

    def test_creator_fallback_when_lane_entered_at_creation(self):
        item = self._item(status='in_review', created_by_id=202, created_by_email='submitter@shizuha.com')
        sub_id, sub_email = WorkflowService._submitter_of_record(item)
        self.assertEqual(sub_id, 202)
        self.assertEqual(sub_email, 'submitter@shizuha.com')
        reasons = WorkflowService._eligibility_reasons_for_candidate(
            item, 'submitter@shizuha.com', user_id=202,
        )
        self.assertIn('submitter_of_record', reasons)
