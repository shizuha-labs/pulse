"""PLS-278: composed routability hold/fail-loud regressions."""
import os
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Comment, Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService


class _FakeUserService:
    users = {
        'ryo@shizuha.com': {'id': 101, 'email': 'ryo@shizuha.com', 'username': 'ryo'},
        'sara@shizuha.com': {'id': 102, 'email': 'sara@shizuha.com', 'username': 'sara'},
    }

    def get_user_by_email(self, email):
        return self.users.get((email or '').strip().lower())


class _MissingUserService:
    def get_user_by_email(self, email):
        return None


class PLS278RoutabilityHoldTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Pulse', key='PLS278', org_id=1, access_level='open')
        WorkflowService._invalidate_team_roster_caches()

    def tearDown(self):
        WorkflowService._invalidate_team_roster_caches()
        WorkflowService._db_avail_cache = None
        WorkflowService._health_avail_cache = None
        WorkflowService._disabled_emails_cache = None

    def _item(self, **kwargs):
        data = dict(
            project=self.project,
            title='routing regression',
            mode='task',
            item_type='task.bug',
            workflow_name='autonomous-bug',
            status='open',
            workflow_status='open',
            assignment_group='engineering',
        )
        data.update(kwargs)
        return Item.objects.create(**data)

    @patch.dict(os.environ, {'PULSE_ROUTABILITY_ENFORCE': '1'})
    @patch('tasks.services.workflow_service.WorkflowService._notify_routability_hold', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_lead_email', return_value='ryo@shizuha.com')
    @patch('tasks.user_service.user_service', _FakeUserService())
    def test_unknown_assignment_group_holds_instead_of_lead_fallback(self, _lead, _notify):
        item = self._item(assignment_group='phantom-team')

        uid, email = WorkflowService._resolve_team_assignee('phantom-team', item=item)

        self.assertEqual((uid, email), (None, None))
        self.assertTrue(Comment.objects.filter(
            item=item,
            author_email='workflow@pulse',
            content__contains='PLS-278 routability hold',
        ).filter(content__contains='unknown/phantom team').exists())

    @patch.dict(os.environ, {
        'PULSE_ROUTABILITY_ENFORCE': '1',
        'PULSE_WIP_CAP': '1',
        'PULSE_INFLIGHT_CAP': '2',
        'PULSE_WIP_CAP_TEAMS': 'engineering',
    })
    @patch('tasks.services.workflow_service.WorkflowService._notify_routability_hold', return_value=True)
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._agent_workable_load', return_value=2)
    @patch('tasks.services.workflow_service.WorkflowService._agent_active_load', return_value=1)
    def test_all_candidates_over_capacity_holds_with_deduped_comment(
        self, _active, _workable, _avail, _can_execute, _notify,
    ):
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={
                'tiers': [['ryo@shizuha.com', 'sara@shizuha.com']],
                'wip_enforced': True,
                'ready_wip_cap': 1,
                'active_wip_cap': 1,
            },
        )
        WorkflowService._invalidate_team_roster_caches()
        self._item(
            title='ryo already owns ready work',
            assignee_id=101,
            assignee_email='ryo@shizuha.com',
        )
        self._item(
            title='sara already owns ready work',
            assignee_id=102,
            assignee_email='sara@shizuha.com',
        )
        item = self._item()

        self.assertEqual(
            WorkflowService._resolve_team_assignee('engineering', enforce_cap=True, item=item),
            (None, None),
        )
        self.assertEqual(
            WorkflowService._resolve_team_assignee('engineering', enforce_cap=True, item=item),
            (None, None),
        )

        qs = Comment.objects.filter(
            item=item,
            author_email='workflow@pulse',
            content__contains='PLS-278 routability hold',
        ).filter(content__contains='over WIP/in-flight cap')
        self.assertEqual(qs.count(), 1)

    # ------------------------------------------------------------------
    # PLS-1280: non-routable item states must not emit routability holds.
    # Live pattern (zen, QA seat, 2026-09-17): items already in_review or
    # done repeatedly held with "seat over WIP/in-flight cap" — assignment
    # cannot change anything in those states, so the hold is noise that
    # buries real holds.

    @patch.dict(os.environ, {
        'PULSE_ROUTABILITY_ENFORCE': '1',
        'PULSE_WIP_CAP': '1',
        'PULSE_INFLIGHT_CAP': '2',
        'PULSE_WIP_CAP_TEAMS': 'engineering',
    })
    @patch('tasks.services.workflow_service.WorkflowService._notify_routability_hold', return_value=True)
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=False)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._agent_workable_load', return_value=2)
    @patch('tasks.services.workflow_service.WorkflowService._agent_active_load', return_value=1)
    def test_status_role_ineligible_item_no_hold_even_over_cap(
        self, _active, _workable, _avail, _can_execute, _notify,
    ):
        """An available team member is seated even when they cannot execute
        the status. That mismatch is not a reason to leave the task
        unassigned or to emit a routability hold."""
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={
                'tiers': [['ryo@shizuha.com']],
                'wip_enforced': True,
                'ready_wip_cap': 1,
                'active_wip_cap': 1,
            },
        )
        WorkflowService._invalidate_team_roster_caches()
        item = self._item(status='in_review', workflow_status='in_review')

        self.assertEqual(
            WorkflowService._resolve_team_assignee('engineering', enforce_cap=True, item=item),
            (101, 'ryo@shizuha.com'),
        )
        self.assertFalse(
            Comment.objects.filter(
                item=item,
                author_email='workflow@pulse',
                content__contains='PLS-278 routability hold',
            ).exists(),
            'seating an available member must not emit a routability hold',
        )

    @patch.dict(os.environ, {'PULSE_ROUTABILITY_ENFORCE': '1'})
    @patch('tasks.services.workflow_service.WorkflowService._notify_routability_hold', return_value=True)
    @patch('tasks.user_service.user_service', _FakeUserService())
    def test_terminal_item_skips_resolver_without_hold(self, _notify):
        """PLS-1280: a terminal item (done) can never be assigned — the
        resolver must skip the whole cycle without emitting a hold (live:
        PLS-1277 held x3 while status done)."""
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={'tiers': [['ryo@shizuha.com']]},
        )
        WorkflowService._invalidate_team_roster_caches()
        item = self._item(status='done', workflow_status='done')

        self.assertEqual(
            WorkflowService._resolve_team_assignee('engineering', enforce_cap=True, item=item),
            (None, None),
        )
        self.assertFalse(
            Comment.objects.filter(
                item=item,
                author_email='workflow@pulse',
                content__contains='PLS-278 routability hold',
            ).exists(),
        )

    @patch.dict(os.environ, {
        'PULSE_ROUTABILITY_ENFORCE': '1',
        'PULSE_WIP_CAP': '1',
        'PULSE_INFLIGHT_CAP': '2',
        'PULSE_WIP_CAP_TEAMS': 'engineering',
    })
    @patch('tasks.services.workflow_service.WorkflowService._notify_routability_hold', return_value=True)
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    # The roster admission path (wip_enforced=True) reads capacity through
    # _agent_slot_load, NOT the legacy _agent_active_load/_agent_workable_load
    # helpers (those feed only the _cap_applies fallback this fixture never
    # takes — patching them left slot_load at the empty-DB 0, the seat looked
    # admissible, and the resolver assigned instead of holding; run 6537).
    @patch('tasks.services.workflow_service.WorkflowService._agent_slot_load', return_value=2)
    def test_waiting_status_admitted_over_cap(self, _slot, _avail, _can_execute, _notify):
        """PLS-843 (hiro ruling, option (i), cmt 978600): waiting statuses that
        are role-excluded for the owning lane do NOT consume admission
        capacity. An over-cap engineering seat is ADMITTED an ``in_review``
        item — the raw ownership cap must not be re-applied on top of the
        role-excluded classification."""
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={
                'tiers': [['ryo@shizuha.com']],
                'wip_enforced': True,
                'ready_wip_cap': 1,
                'active_wip_cap': 1,
            },
        )
        WorkflowService._invalidate_team_roster_caches()
        item = self._item(status='in_review', workflow_status='in_review')

        uid, email = WorkflowService._resolve_team_assignee(
            'engineering', enforce_cap=True, item=item,
        )
        self.assertEqual(
            (uid, email), (101, 'ryo@shizuha.com'),
            'an over-cap seat must still be admitted a role-excluded waiting item',
        )
        self.assertFalse(
            Comment.objects.filter(
                item=item,
                author_email='workflow@pulse',
                content__contains='PLS-278 routability hold',
            ).filter(content__contains='over WIP/in-flight cap').exists(),
            'role-excluded waiting status must not emit an over-cap hold',
        )

    @patch.dict(os.environ, {
        'PULSE_ROUTABILITY_ENFORCE': '1',
        'PULSE_WIP_CAP': '1',
        'PULSE_INFLIGHT_CAP': '2',
        'PULSE_WIP_CAP_TEAMS': 'engineering',
    })
    @patch('tasks.services.workflow_service.WorkflowService._notify_routability_hold', return_value=True)
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._agent_slot_load', return_value=2)
    def test_capacity_consuming_status_still_holds_over_cap(self, _slot, _avail, _can_execute, _notify):
        """PLS-843 opposite polarity (kept from the former
        test_role_eligible_over_cap_item_still_holds): the same over-cap seat
        is still HELD for a capacity-consuming ``in_progress`` item — the
        waiting-status admission carve-out must not become over-suppression."""
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={
                'tiers': [['ryo@shizuha.com']],
                'wip_enforced': True,
                'ready_wip_cap': 1,
                'active_wip_cap': 1,
            },
        )
        WorkflowService._invalidate_team_roster_caches()
        item = self._item(status='in_progress', workflow_status='in_progress')

        self.assertEqual(
            WorkflowService._resolve_team_assignee('engineering', enforce_cap=True, item=item),
            (None, None),
        )
        self.assertTrue(
            Comment.objects.filter(
                item=item,
                author_email='workflow@pulse',
                content__contains='PLS-278 routability hold',
            ).filter(content__contains='over WIP/in-flight cap').exists(),
        )

    def test_default_flag_is_shadow_not_enforce(self):
        """PLS-277 HLD §10 rollout-safety (kei/revi review): the resolver — which
        assigns ALL fleet work — must DEFAULT to shadow so a false-positive verdict
        cannot hold real work fleet-wide on deploy. Only an explicit truthy flag
        enforces; the bake flips it after confirming no false holds."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PULSE_ROUTABILITY_ENFORCE', None)
            self.assertFalse(
                WorkflowService._routability_enforced(),
                'default (unset) PULSE_ROUTABILITY_ENFORCE must be SHADOW, not enforce',
            )
        for val in ('shadow', 'shadow-only', 'off', '0', 'false', 'disabled'):
            with patch.dict(os.environ, {'PULSE_ROUTABILITY_ENFORCE': val}):
                self.assertFalse(WorkflowService._routability_enforced(), f'{val!r} must not enforce')
        for val in ('1', 'true', 'on', 'enforce'):
            with patch.dict(os.environ, {'PULSE_ROUTABILITY_ENFORCE': val}):
                self.assertTrue(WorkflowService._routability_enforced(), f'{val!r} must enforce')

    @patch.dict(os.environ, {'PULSE_ROUTABILITY_ENFORCE': '1'})
    @patch('tasks.services.workflow_service.WorkflowService._notify_routability_hold', return_value=True)
    @patch('tasks.user_service.user_service', _FakeUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_human_account', return_value=False)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=False)
    def test_seat_not_live_holds_under_enforce(self, _avail, _human, _can, _notify):
        """HLD §9: a staffed team whose every seat is not live for new assignment
        (empty-turn / auth-429 / broker-token-unavailable — all folded into
        _is_agent_available_for_assignment) holds + fails loud, never phantom-assigns."""
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={'tiers': [['ryo@shizuha.com', 'sara@shizuha.com']]},
        )
        WorkflowService._invalidate_team_roster_caches()
        item = self._item()

        self.assertEqual(
            WorkflowService._resolve_team_assignee('engineering', item=item),
            (None, None),
        )
        self.assertTrue(Comment.objects.filter(
            item=item,
            author_email='workflow@pulse',
            content__contains='PLS-278 routability hold',
        ).filter(content__contains='seat not live').exists())

    @patch.dict(os.environ, {'PULSE_ROUTABILITY_ENFORCE': '1'})
    @patch('tasks.services.workflow_service.WorkflowService._notify_routability_hold', return_value=True)
    @patch('tasks.user_service.user_service', _MissingUserService())
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_human_account', return_value=False)
    @patch('tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment', return_value=True)
    def test_identity_lookup_failure_is_not_mislabeled_as_workflow_role_ineligible(
        self, _avail, _human, _can, _notify,
    ):
        """CTX-607: a directory miss is a resolver-source failure, not proof
        that a Team member lacks permission to execute the workflow status."""
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            defaults={'tiers': [['ryo@shizuha.com', 'sara@shizuha.com']]},
        )
        WorkflowService._invalidate_team_roster_caches()
        item = self._item()

        self.assertEqual(
            WorkflowService._resolve_team_assignee('engineering', item=item),
            (None, None),
        )

        hold = Comment.objects.get(
            item=item,
            author_email='workflow@pulse',
            content__contains='PLS-278 routability hold',
        )
        self.assertIn('resolver source unavailable', hold.content)
        self.assertIn('`ryo@shizuha.com` → resolver_source_unavailable', hold.content)
        self.assertIn('`sara@shizuha.com` → resolver_source_unavailable', hold.content)
        self.assertNotIn('seat cannot execute this workflow status', hold.content)
