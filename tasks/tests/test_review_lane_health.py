"""PLAT-7062: review-lane health probe tests."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project
from tasks.services.review_lane_health import check_review_lane_health


def _make_review_item(*, priority='urgent', updated_hours_ago=2, key='RVW'):
    proj = Project.objects.create(name='Review Lane', key='RVWK', org_id=1)
    item = Item.objects.create(
        project=proj, mode='task', status='in_progress', title=f'{key}-1',
        priority=priority, assignment_group='review',
    )
    # Item.save() normalizes status via the workflow system and auto_now
    # overrides updated_at; force the review status + stall age via a queryset
    # update so the probe's filters match (real review items carry
    # status='in_review' set through workflow transitions).
    Item.objects.filter(pk=item.pk).update(
        status='in_review', workflow_status='In Review',
        updated_at=timezone.now() - timedelta(hours=updated_hours_ago),
        created_at=timezone.now() - timedelta(hours=updated_hours_ago),
    )
    item.refresh_from_db()
    return item


def _idle_seat(email='revi@shizuha.com'):
    return {'email': email, 'live': True, 'reasons': [], 'over_capacity': False}


def _busy_seat(email='revi@shizuha.com'):
    return {'email': email, 'live': True, 'reasons': [], 'over_capacity': True}


def _down_seat(email='revi@shizuha.com'):
    return {'email': email, 'live': False, 'reasons': ['seat_not_live'],
            'over_capacity': False}


class CheckReviewLaneHealthTests(TestCase):
    @mock.patch('tasks.services.review_lane_health._review_team_roster',
                return_value=['revi@shizuha.com'])
    @mock.patch('tasks.services.review_lane_health._seat_state',
                side_effect=lambda email, probe: _idle_seat(email))
    @mock.patch('tasks.services.review_lane_health._fire_review_lane_alert')
    def test_desync_when_idle_seat_exists(self, mock_fire, mock_seat, mock_roster):
        _make_review_item(updated_hours_ago=2)
        result = check_review_lane_health(stall_hours=1)
        self.assertEqual(result['checked'], 1)
        self.assertEqual(len(result['desync']), 1)
        self.assertEqual(len(result['starvation']), 0)
        self.assertEqual(result['escalated'], 1)
        mock_fire.assert_called_once()
        self.assertEqual(mock_fire.call_args[1]['kind'], 'desync')
        self.assertEqual(mock_fire.call_args[1]['live_idle'], 1)

    @mock.patch('tasks.services.review_lane_health._review_team_roster',
                return_value=['revi@shizuha.com'])
    @mock.patch('tasks.services.review_lane_health._seat_state',
                side_effect=lambda email, probe: _busy_seat(email))
    @mock.patch('tasks.services.review_lane_health._fire_review_lane_alert')
    def test_starvation_when_all_seats_busy(self, mock_fire, mock_seat, mock_roster):
        _make_review_item(updated_hours_ago=2)
        result = check_review_lane_health(stall_hours=1)
        self.assertEqual(len(result['starvation']), 1)
        self.assertEqual(len(result['desync']), 0)
        self.assertEqual(result['escalated'], 1)
        self.assertEqual(mock_fire.call_args[1]['kind'], 'starvation')
        self.assertEqual(mock_fire.call_args[1]['live_idle'], 0)
        self.assertEqual(mock_fire.call_args[1]['live_busy'], 1)

    @mock.patch('tasks.services.review_lane_health._review_team_roster',
                return_value=['revi@shizuha.com'])
    @mock.patch('tasks.services.review_lane_health._seat_state',
                side_effect=lambda email, probe: _down_seat(email))
    @mock.patch('tasks.services.review_lane_health._fire_review_lane_alert')
    def test_starvation_when_all_seats_down(self, mock_fire, mock_seat, mock_roster):
        _make_review_item(updated_hours_ago=2)
        result = check_review_lane_health(stall_hours=1)
        self.assertEqual(len(result['starvation']), 1)
        self.assertEqual(result['escalated'], 1)
        self.assertEqual(mock_fire.call_args[1]['kind'], 'starvation')
        self.assertEqual(mock_fire.call_args[1]['down'], 1)

    @mock.patch('tasks.services.review_lane_health._review_team_roster',
                return_value=['revi@shizuha.com'])
    @mock.patch('tasks.services.review_lane_health._seat_state',
                side_effect=lambda email, probe: _idle_seat(email))
    @mock.patch('tasks.services.review_lane_health._fire_review_lane_alert')
    def test_skips_item_within_threshold(self, mock_fire, mock_seat, mock_roster):
        _make_review_item(updated_hours_ago=0.1)
        result = check_review_lane_health(stall_hours=1)
        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['escalated'], 0)
        mock_fire.assert_not_called()

    @mock.patch('tasks.services.review_lane_health._review_team_roster',
                return_value=['revi@shizuha.com'])
    @mock.patch('tasks.services.review_lane_health._seat_state',
                side_effect=lambda email, probe: _idle_seat(email))
    @mock.patch('tasks.services.review_lane_health._fire_review_lane_alert')
    def test_skips_non_review_status(self, mock_fire, mock_seat, mock_roster):
        proj = Project.objects.create(name='Review Lane', key='RVW2', org_id=1)
        Item.objects.create(
            project=proj, mode='task', status='in_progress', title='RVW2-1',
            priority='urgent', assignment_group='review',
        )
        result = check_review_lane_health(stall_hours=1)
        self.assertEqual(result['checked'], 0)
        mock_fire.assert_not_called()

    @mock.patch('tasks.services.review_lane_health._review_team_roster',
                return_value=['revi@shizuha.com'])
    @mock.patch('tasks.services.review_lane_health._seat_state',
                side_effect=lambda email, probe: _idle_seat(email))
    @mock.patch('tasks.services.review_lane_health._fire_review_lane_alert')
    def test_skips_non_critical_priority(self, mock_fire, mock_seat, mock_roster):
        _make_review_item(priority='normal', updated_hours_ago=2)
        result = check_review_lane_health(stall_hours=1)
        self.assertEqual(result['checked'], 0)
        mock_fire.assert_not_called()

    @mock.patch('tasks.services.review_lane_health._review_team_roster',
                return_value=['revi@shizuha.com'])
    @mock.patch('tasks.services.review_lane_health._seat_state',
                side_effect=lambda email, probe: _idle_seat(email))
    @mock.patch('tasks.services.review_lane_health._fire_review_lane_alert')
    def test_no_waiting_items_no_escalation(self, mock_fire, mock_seat, mock_roster):
        result = check_review_lane_health(stall_hours=1)
        self.assertEqual(result['checked'], 0)
        self.assertEqual(result['escalated'], 0)
        mock_fire.assert_not_called()
