"""PLS-1365: resolved_elsewhere with a terminal linked parent must not be
force-pulled by critical-unassigned-override or the rebalance capacity drain.

Fixture shape (PLAT-9643 / parent PLAT-9587):
  - blocker child sits in non-terminal ``resolved_elsewhere`` (PLS-719
    block-releasing token; acceptance met once the parent completes);
  - linked parent has already reached a terminal status (``completed``);
  - child is urgent/high, unassigned, so the critical override historically
    treated it as actionable and force-assigned it ~8×/12h.

Mechanism 1: treat that shape as effect-terminal for assignment.
Mechanism 3: rate-limit force-pulls inside the boundary window without a
status change (defense-in-depth for other non-terminal shapes).
"""
import os
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from tasks.models import Comment, Item, ItemLink, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService


class AssignmentEffectTerminalUnitTests(SimpleTestCase):
    def test_non_resolved_elsewhere_is_not_effect_terminal(self):
        item = Item(status='todo')
        with patch.object(
            WorkflowService, '_resolved_elsewhere_blocker_parents', return_value=[],
        ):
            self.assertFalse(WorkflowService._is_assignment_effect_terminal(item))

    def test_resolved_elsewhere_without_parents_is_not_effect_terminal(self):
        item = Item(status='resolved_elsewhere')
        with patch.object(
            WorkflowService, '_resolved_elsewhere_blocker_parents', return_value=[],
        ):
            self.assertFalse(WorkflowService._is_assignment_effect_terminal(item))

    def test_resolved_elsewhere_with_live_parent_is_not_effect_terminal(self):
        parent = Item(status='in_progress')
        item = Item(status='resolved_elsewhere')
        with patch.object(
            WorkflowService,
            '_resolved_elsewhere_blocker_parents',
            return_value=[(parent, object())],
        ):
            self.assertFalse(WorkflowService._is_assignment_effect_terminal(item))

    def test_resolved_elsewhere_with_terminal_parent_is_effect_terminal(self):
        parent = Item(status='completed')
        item = Item(status='resolved_elsewhere')
        with patch.object(
            WorkflowService,
            '_resolved_elsewhere_blocker_parents',
            return_value=[(parent, object())],
        ):
            self.assertTrue(WorkflowService._is_assignment_effect_terminal(item))


class Plat9643CriticalOverrideRegressionTests(TestCase):
    """PLAT-9643 shape: parent completed, child resolved_elsewhere, no re-pull."""

    @patch.dict(os.environ, {
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
        'PULSE_WIP_CAP': '1',
        'PULSE_INFLIGHT_CAP': '1',
        'PULSE_CRITICAL_OVERRIDE_DWELL_MINUTES': '30',
        'PULSE_FORCE_PULL_BOUNDARY_MINUTES': '30',
    })
    def test_resolved_elsewhere_child_with_completed_parent_not_force_assigned(self):
        project = Project.objects.create(
            org_id=1,
            name='PLS-1365 PLAT-9643',
            key='P9643',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['kai@shizuha.com']],
                'is_active': True,
            },
        )

        parent = Item.objects.create(
            title='PLAT-9587 parent completed',
            mode='task',
            status='done',
            priority='urgent',
            assignment_group='engineering',
            workflow_name='simple',
            project=project,
            assignee_id=1,
            assignee_email='kai@shizuha.com',
        )
        child = Item.objects.create(
            title='PLAT-9643 resolved_elsewhere child',
            mode='task',
            status='resolved_elsewhere',
            priority='urgent',
            assignment_group='engineering',
            workflow_name='simple',
            project=project,
            assignee_id=None,
            assignee_email='',
        )
        link = ItemLink(
            source_item=child,
            target_item=parent,
            link_type='blocks',
            created_by_id=0,
            created_by_email='test@pulse',
        )
        link._skip_auto_block = True
        link.save()

        self.assertTrue(WorkflowService._is_assignment_effect_terminal(child))

        class FakeUserService:
            def get_user_by_email(self, email):
                return {'id': 502, 'email': email} if email == 'kai@shizuha.com' else None

            def get_user_by_id(self, user_id):
                return (
                    {'id': 502, 'email': 'kai@shizuha.com'}
                    if int(user_id) == 502 else None
                )

        with patch('tasks.user_service.user_service', FakeUserService()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          return_value=True):
            from tasks.tasks import _assign_critical_unassigned
            assigned_count = _assign_critical_unassigned()

        child.refresh_from_db()
        self.assertEqual(assigned_count, 0)
        self.assertFalse((child.assignee_email or '').strip())
        self.assertIsNone(child.assignee_id)
        self.assertFalse(
            Comment.objects.filter(
                item=child,
                content__startswith='🚨 **Critical unassigned override**',
            ).exists(),
        )

    @patch.dict(os.environ, {
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
        'PULSE_WIP_CAP': '1',
        'PULSE_INFLIGHT_CAP': '1',
    })
    def test_resolved_elsewhere_child_excluded_from_capacity_queue(self):
        project = Project.objects.create(
            org_id=1,
            name='PLS-1365 capacity queue',
            key='P1365Q',
        )
        parent = Item.objects.create(
            title='terminal parent',
            mode='task',
            status='done',
            priority='high',
            assignment_group='engineering',
            workflow_name='simple',
            project=project,
        )
        child = Item.objects.create(
            title='effect-terminal child',
            mode='task',
            status='resolved_elsewhere',
            priority='high',
            assignment_group='engineering',
            workflow_name='simple',
            project=project,
            assignee_id=None,
            assignee_email='',
        )
        link = ItemLink(
            source_item=child,
            target_item=parent,
            link_type='blocks',
            created_by_id=0,
            created_by_email='test@pulse',
        )
        link._skip_auto_block = True
        link.save()

        live = Item.objects.create(
            title='live queued work',
            mode='task',
            status='todo',
            priority='high',
            assignment_group='engineering',
            workflow_name='simple',
            project=project,
            assignee_id=None,
            assignee_email='',
        )

        queued = WorkflowService._queued_tasks_for_team(
            'engineering', organization_id=1,
        )
        keys = {getattr(it, 'item_key', None) or it.pk for it in queued}
        self.assertIn(live.pk, keys)
        self.assertNotIn(child.pk, keys)


class ForcePullBoundaryRateLimitTests(TestCase):
    def test_recent_override_without_status_change_blocks_re_pull(self):
        project = Project.objects.create(
            org_id=1, name='PLS-1365 rate limit', key='P1365R',
        )
        item = Item.objects.create(
            title='rate-limited item',
            mode='task',
            status='todo',
            priority='urgent',
            assignment_group='engineering',
            workflow_name='simple',
            project=project,
        )
        c = Comment.objects.create(
            item=item,
            author_id=0,
            author_email='system@pulse',
            content=(
                '🚨 **Critical unassigned override**: force-assigned to '
                '**kai@shizuha.com** (engineering) because this urgent/high '
                'item had no owner and was in a normally-excluded status '
                '(`blocked`). WIP cap overridden for this item.'
            ),
        )
        Comment.objects.filter(pk=c.pk).update(
            created_at=timezone.now() - timedelta(minutes=5),
        )
        self.assertTrue(
            WorkflowService._force_pull_blocked_by_boundary(item, window_minutes=30),
        )

    def test_status_change_after_force_pull_clears_boundary(self):
        from tasks.models import ItemActivity

        project = Project.objects.create(
            org_id=1, name='PLS-1365 rate clear', key='P1365C',
        )
        item = Item.objects.create(
            title='status-changed after pull',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='engineering',
            workflow_name='simple',
            project=project,
        )
        c = Comment.objects.create(
            item=item,
            author_id=0,
            author_email='system@pulse',
            content=(
                'Pulled from the **engineering** capacity queue to '
                '**kai@shizuha.com** (rebalance sweep found headroom — '
                'PLAT-236/PLAT-867).'
            ),
        )
        Comment.objects.filter(pk=c.pk).update(
            created_at=timezone.now() - timedelta(minutes=5),
        )
        act = ItemActivity.objects.create(
            item=item,
            action='status_changed',
            field_name='status',
            old_value='blocked',
            new_value='open',
            user_id=0,
            user_email='system@pulse',
        )
        ItemActivity.objects.filter(pk=act.pk).update(
            created_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertFalse(
            WorkflowService._force_pull_blocked_by_boundary(item, window_minutes=30),
        )
