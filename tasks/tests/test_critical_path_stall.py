"""PLS-1045: critical-path stall probe tests."""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from tasks.models import Item, Project
from tasks.services.critical_path_stall import check_critical_path_stalls


def _make_item(*, priority='urgent', status='todo', updated_hours_ago=2,
               key='CP'):
    proj = Project.objects.create(name='CP Stall', key='CPK', org_id=1)
    item = Item.objects.create(
        project=proj, mode='task', status=status, title=f'{key}-1',
        priority=priority,
    )
    # Item.save() auto_now overrides updated_at; force the stall age via a
    # queryset update so the threshold matches.
    Item.objects.filter(pk=item.pk).update(
        updated_at=timezone.now() - timedelta(hours=updated_hours_ago),
        created_at=timezone.now() - timedelta(hours=updated_hours_ago),
    )
    item.refresh_from_db()
    return item


class CheckCriticalPathStallsTests(TestCase):
    @mock.patch('tasks.services.critical_path_stall._seat_available', return_value=False)
    @mock.patch('tasks.services.critical_path_stall._fire_stall_escalation')
    def test_escalates_stranded_item_on_down_seat(self, mock_fire, mock_seat):
        item = _make_item(updated_hours_ago=2)
        result = check_critical_path_stalls(stall_hours=1)
        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['escalated'], 1)
        mock_fire.assert_called_once()
        args = mock_fire.call_args[0]
        kwargs = mock_fire.call_args[1]
        self.assertEqual(args[0].pk, item.pk)
        self.assertIs(kwargs['seat_available'], False)
        self.assertEqual(kwargs['reason'], 'seat_down_or_saturated')

    @mock.patch('tasks.services.critical_path_stall._seat_available', return_value=True)
    @mock.patch('tasks.services.critical_path_stall._fire_stall_escalation')
    def test_escalates_stranded_item_with_no_movement(self, mock_fire, mock_seat):
        _make_item(updated_hours_ago=2)
        result = check_critical_path_stalls(stall_hours=1)
        self.assertEqual(result['escalated'], 1)
        kwargs = mock_fire.call_args[1]
        self.assertEqual(kwargs['reason'], 'no_movement')

    @mock.patch('tasks.services.critical_path_stall._seat_available', return_value=None)
    @mock.patch('tasks.services.critical_path_stall._fire_stall_escalation')
    def test_surfaces_seat_availability_chain_desync(self, mock_fire, mock_seat):
        _make_item(updated_hours_ago=2)
        result = check_critical_path_stalls(stall_hours=1)
        self.assertEqual(result['escalated'], 1)
        kwargs = mock_fire.call_args[1]
        self.assertIsNone(kwargs['seat_available'])
        self.assertEqual(kwargs['reason'], 'seat_availability_unknown')

    @mock.patch('tasks.services.critical_path_stall._seat_available')
    @mock.patch('tasks.services.critical_path_stall._fire_stall_escalation')
    def test_skips_item_within_threshold(self, mock_fire, mock_seat):
        _make_item(updated_hours_ago=0.1)
        result = check_critical_path_stalls(stall_hours=1)
        self.assertEqual(result['checked'], 1)
        self.assertEqual(result['escalated'], 0)
        mock_fire.assert_not_called()

    @mock.patch('tasks.services.critical_path_stall._seat_available')
    @mock.patch('tasks.services.critical_path_stall._fire_stall_escalation')
    def test_skips_non_critical_priority(self, mock_fire, mock_seat):
        _make_item(priority='normal', updated_hours_ago=2)
        result = check_critical_path_stalls(stall_hours=1)
        self.assertEqual(result['checked'], 0)
        mock_fire.assert_not_called()

    @mock.patch('tasks.services.critical_path_stall._seat_available')
    @mock.patch('tasks.services.critical_path_stall._fire_stall_escalation')
    def test_skips_terminal_status(self, mock_fire, mock_seat):
        _make_item(status='done', updated_hours_ago=2)
        result = check_critical_path_stalls(stall_hours=1)
        self.assertEqual(result['checked'], 0)
        mock_fire.assert_not_called()
