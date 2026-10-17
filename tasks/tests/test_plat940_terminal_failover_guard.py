"""PLAT-940: the failover/rebalance drain must never re-route a TERMINAL-status task.

PR #191 added (1) 'applied' (change-request terminal) to the canonical terminal set,
and (2) a `_is_terminal_status` guard in sweep_rebalance_pods' drain loop that skips
any terminal item that slipped through drain_all_items' status-exclusion (race, or a
slug like 'applied'/'merged' not in _assignment_excluded_statuses). Regression for both.
"""
from unittest import mock
from django.test import TestCase
from tasks.services.workflow_service import WorkflowService


class TerminalSlugTests(TestCase):
    def test_applied_and_merged_are_terminal(self):
        from tasks.tasks import _is_terminal_status
        # 'applied' (change-request final state) must be terminal (PR #191 part 1)
        self.assertTrue(_is_terminal_status('applied'))
        # 'merged' is terminal in the canonical enum (guard relies on this)
        self.assertTrue(_is_terminal_status('merged'))
        self.assertFalse(_is_terminal_status('in_progress'))


class TerminalFailoverGuardTests(TestCase):
    def test_terminal_task_not_failed_over_from_down_agent(self):
        from django.contrib.auth import get_user_model
        from tasks.models import Item, Project, TeamRoster
        from tasks.tasks import sweep_rebalance_pods

        User = get_user_model()
        down = User.objects.create_user(username='downa', email='downa@shizuha.com', password='x')
        peer = User.objects.create_user(username='peera', email='peera@shizuha.com', password='x')
        project = Project.objects.create(name='Plat940', key='P940', org_id=1)
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[down.email, peer.email]],
                'is_active': True,
            },
        )
        # An 'applied' (terminal) task assigned to the DOWN agent. 'applied' is NOT in
        # _assignment_excluded_statuses, so it survives drain_all_items' .exclude() and
        # lands in `picks` — the new _is_terminal_status guard must then skip it.
        # Set the status via queryset .update(): Item.save() heals slugs that are
        # invalid for the item's workflow ('applied' isn't in 'simple'), which would
        # rewrite it to 'todo' and make this test vacuous. The guard exists precisely
        # for rows that reach the drain with a slug save() never validated (race /
        # out-of-band writer), which .update() reproduces.
        applied = Item.objects.create(
            title='terminal applied task', project=project, assignment_group='engineering',
            assignee_id=down.id, assignee_email='downa@shizuha.com', mode='task',
        )
        Item.objects.filter(pk=applied.pk).update(status='applied')
        applied.refresh_from_db()
        self.assertEqual(applied.status, 'applied')

        def _user_by_email(email):
            n = (email or '').strip().lower()
            if n == 'downa@shizuha.com':
                return {'id': down.id, 'email': down.email}
            if n == 'peera@shizuha.com':
                return {'id': peer.id, 'email': peer.email}
            return None

        with mock.patch.object(
            WorkflowService, '_pod_roster',
            return_value={'engineering': ['downa@shizuha.com', 'peera@shizuha.com']},
        ), mock.patch.object(
            WorkflowService, '_disabled_agent_emails', return_value={'downa@shizuha.com'},
        ), mock.patch(
            'tasks.user_service.user_service.get_user_by_email', side_effect=_user_by_email,
        ), mock.patch.dict(
            'os.environ',
            {'PULSE_HEALTH_EXPORTER_URL': '', 'PULSE_API_HEALTH_URL': 'disabled'},
            clear=False,
        ):
            sweep_rebalance_pods.apply(args=[], kwargs={'max_skew': 0})

        applied.refresh_from_db()
        # The terminal task must remain on the down agent — never failed over.
        self.assertEqual(applied.assignee_email, 'downa@shizuha.com')
        self.assertEqual(applied.assignee_id, down.id)



class Hive626DirectReviewHoldTests(TestCase):
    def test_predicate_holds_architecture_review_on_engineering_lane(self):
        from types import SimpleNamespace
        from tasks.tasks import _hive626_direct_review_hold

        item = SimpleNamespace(status='in_progress', labels=['architecture-review'], tags=[])
        with mock.patch.object(WorkflowService, '_is_architecture_team_email', return_value=True):
            self.assertTrue(_hive626_direct_review_hold(item, 'arch@shizuha.com', 'engineering'))
        self.assertFalse(_hive626_direct_review_hold(
            SimpleNamespace(status='open', labels=['architecture-review'], tags=[]),
            'arch@shizuha.com', 'engineering',
        ))
        with mock.patch.object(WorkflowService, '_is_architecture_team_email', return_value=False):
            self.assertFalse(_hive626_direct_review_hold(item, 'eng@shizuha.com', 'engineering'))
        self.assertFalse(_hive626_direct_review_hold(item, 'arch@shizuha.com', 'review'))
