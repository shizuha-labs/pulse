"""PLS-1382 / PLS-1385: rebalance/cap machinery must not cycle terminal or
settled (resolved_elsewhere) records into assignment or emit routability holds
against them.

Live specimens
--------------
* PLAT-9072 (PLS-1382): status ``resolved_elsewhere`` was force-assigned by the
  critical-unassigned override (comment 925285 — "normally-excluded status"),
  returned by PLS-509 reclaim, then re-pulled by the capacity drain. Cycle
  burned merge-seat beats every ~30–40 min.
* SCLI-707 / ORIG-377 / … (PLS-1385): ``rebalance-capacity-reservation`` and
  ``team-resolver`` emitted phantom routability holds against ``done`` tasks.

Fix surface
-----------
1. ``resolved_elsewhere`` ∈ ``_assignment_excluded_statuses`` → capacity-pull
   queue + WIP load never see it.
2. Critical override ``_TERMINAL`` includes ``resolved_elsewhere`` → force-assign
   refuses it even though it is not a true terminal.
3. ``_record_routability_hold`` centrally suppresses holds when
   ``_routability_hold_state_non_routable`` (terminals + resolved_elsewhere).
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from django.test import TestCase, SimpleTestCase

from tasks.models import Comment, Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService


class AssignmentExcludedResolvedElsewhereTests(SimpleTestCase):
    def test_resolved_elsewhere_in_assignment_excluded(self):
        excluded = WorkflowService._assignment_excluded_statuses()
        self.assertIn(
            'resolved_elsewhere', excluded,
            'PLS-1382: settled blocker children must not count as WIP / capacity demand',
        )

    def test_pls1280_treats_resolved_elsewhere_as_non_routable(self):
        item = type('I', (), {'status': 'resolved_elsewhere', 'workflow_status': ''})()
        self.assertTrue(
            WorkflowService._routability_hold_state_non_routable(item),
            'PLS-1382: resolved_elsewhere must suppress routability holds',
        )

    def test_pls1280_still_treats_done_as_non_routable(self):
        item = type('I', (), {'status': 'done', 'workflow_status': ''})()
        self.assertTrue(WorkflowService._routability_hold_state_non_routable(item))


class CriticalOverrideSkipsResolvedElsewhereTests(TestCase):
    """PLAT-9072 shape: urgent + unassigned + resolved_elsewhere must NOT be
    force-assigned by `_assign_critical_unassigned`."""

    @patch.dict(os.environ, {
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'merge',
        'PULSE_WIP_CAP': '5',
        'PULSE_INFLIGHT_CAP': '15',
        'PULSE_CRITICAL_OVERRIDE_DWELL_MINUTES': '0',
    })
    def test_resolved_elsewhere_not_force_assigned(self):
        project = Project.objects.create(
            org_id=1, name='PLS-1382 Cap Exclusion', key='P1382',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='merge',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['kei@shizuha.com']],
                'is_active': True,
            },
        )
        item = Item.objects.create(
            title='PLAT-9072 specimen shape',
            mode='task',
            status='resolved_elsewhere',
            workflow_status='resolved_elsewhere',
            priority='urgent',
            assignment_group='merge',
            workflow_name='simple',
            project=project,
        )

        class FakeUserService:
            def get_user_by_email(self, email):
                return {'id': 901, 'email': email} if email == 'kei@shizuha.com' else None

            def get_user_by_id(self, user_id):
                return (
                    {'id': 901, 'email': 'kei@shizuha.com'}
                    if int(user_id) == 901 else None
                )

        with patch('tasks.user_service.user_service', FakeUserService()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          return_value=True):
            from tasks.tasks import _assign_critical_unassigned
            assigned = _assign_critical_unassigned()

        item.refresh_from_db()
        self.assertEqual(assigned, 0)
        self.assertFalse((item.assignee_email or '').strip())
        self.assertIsNone(item.assignee_id)
        self.assertFalse(
            Comment.objects.filter(
                item=item,
                content__startswith='🚨 **Critical unassigned override**',
            ).exists(),
        )


class CapacityQueueExcludesSettledTests(TestCase):
    def test_queued_tasks_for_team_drops_resolved_elsewhere_and_done(self):
        project = Project.objects.create(
            org_id=1, name='PLS-1382 Queue', key='P1382Q',
        )
        for key, status in (
            ('P1382Q-1', 'todo'),
            ('P1382Q-2', 'resolved_elsewhere'),
            ('P1382Q-3', 'done'),
        ):
            Item.objects.create(
                project=project, item_key=key, title=key, mode='task',
                item_type='task.generic', workflow_name='simple',
                workflow_status=status, status=status,
                assignment_group='engineering',
                assignee_email='', created_by_email='system@pulse', created_by_id=0,
            )
        queued = list(WorkflowService._queued_tasks_for_team('engineering'))
        keys = {i.item_key for i in queued}
        self.assertIn('P1382Q-1', keys)
        self.assertNotIn('P1382Q-2', keys)
        self.assertNotIn('P1382Q-3', keys)


class RoutabilityHoldCentralSuppressionTests(TestCase):
    """PLS-1385: `_record_routability_hold` itself must refuse terminal /
    resolved_elsewhere items regardless of caller source."""

    def _item(self, status):
        project = Project.objects.create(
            org_id=1, name=f'Hold {status}', key=f'H{status[:4].upper()}',
        )
        return Item.objects.create(
            title=f'hold specimen {status}',
            mode='task',
            status=status,
            workflow_status=status,
            priority='high',
            assignment_group='architecture',
            workflow_name='simple',
            project=project,
        )

    def test_no_hold_for_done_via_rebalance_capacity_reservation(self):
        item = self._item('done')
        WorkflowService._record_routability_hold(
            item, 'architecture', ['over_capacity'],
            source='rebalance-capacity-reservation',
            attempted_email='hiro@shizuha.com',
            candidate_reasons=[('hiro@shizuha.com', ('over_capacity',))],
        )
        self.assertFalse(
            Comment.objects.filter(
                item=item, content__contains='routability hold',
            ).exists(),
            'PLS-1385: done must not emit a rebalance-capacity-reservation hold',
        )

    def test_no_hold_for_resolved_elsewhere(self):
        item = self._item('resolved_elsewhere')
        WorkflowService._record_routability_hold(
            item, 'merge', ['over_capacity'],
            source='rebalance-capacity-pull',
            attempted_email='kei@shizuha.com',
        )
        self.assertFalse(
            Comment.objects.filter(
                item=item, content__contains='routability hold',
            ).exists(),
        )

    def test_live_over_cap_hold_still_emits(self):
        """Control: a live actionable item still gets the queue-pressure hold."""
        item = self._item('todo')
        WorkflowService._record_routability_hold(
            item, 'engineering', ['over_capacity'],
            source='rebalance-capacity-reservation',
            attempted_email='kai@shizuha.com',
            candidate_reasons=[('kai@shizuha.com', ('over_capacity',))],
        )
        self.assertTrue(
            Comment.objects.filter(
                item=item, content__contains='routability hold',
            ).exists(),
            'control: live todo over-cap hold must still fire',
        )


class OverrideWatchdogForceAssignCountTests(SimpleTestCase):
    """PLAT-10519: 3 same-window force-assigns alert; 2 do not.

    ``_assign_critical_unassigned`` never calls ``record_direct_handoff``, so
    the watchdog must count ``Critical unassigned override`` notices in the
    same window as direct-handoff audits (architecture review on pulse#759).
    """

    def _evaluate(self, count):
        item = type('I', (), {'pk': 1, 'item_key': 'PLAT-10519'})()
        qs = MagicMock()
        qs.count.return_value = count
        with patch.dict(os.environ, {
            'OVERRIDE_WATCHDOG_THRESHOLD': '2',
            'OVERRIDE_WATCHDOG_WINDOW_HOURS': '24',
        }):
            with patch('tasks.views.Comment.objects.filter', return_value=qs) as filt:
                with patch(
                    'tasks.services.deduplication.AlertDeduplicationService.submit_alert',
                    return_value=(object(), True),
                ) as submit:
                    from tasks.views import _watch_override_refires
                    result = _watch_override_refires(item, actor_email='system@pulse')
        return result, filt, submit

    def test_two_notices_do_not_alert(self):
        result, filt, submit = self._evaluate(2)
        self.assertIsNone(result)
        submit.assert_not_called()
        rendered = str(filt.call_args)
        self.assertIn('Critical unassigned override', rendered)
        self.assertIn('Direct assignee override used during task', rendered)

    def test_third_notice_alerts(self):
        result, _filt, submit = self._evaluate(3)
        self.assertIsNotNone(result)
        submit.assert_called_once()
        payload = submit.call_args.args[0]
        self.assertEqual(payload['source'], 'pulse.override_watchdog')
        self.assertEqual(payload['correlation_id'], 'override-watchdog-PLAT-10519')
        self.assertIn('3x', payload['description'])
