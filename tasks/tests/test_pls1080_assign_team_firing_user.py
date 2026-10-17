"""PLS-1080 residual regression: assign_team must never route to the firing
user when they are not a member of the destination team roster.

A governance/admin user firing an Approve that pins assign_team:engineering
must not become the engineering assignee — the org guard then truthfully
rejects the tuple (not-in-team) instead of a real roster member being picked.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import (
    Item,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService


class AssignTeamFiringUserExclusionTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.governance_user = users.objects.create_user(
            username='pls1080-gov',
            email='sora@shizuha.com',
            password='x',
        )
        self.engineer = users.objects.create_user(
            username='pls1080-eng',
            email='nagi@shizuha.com',
            password='x',
        )
        self.project = Project.objects.create(
            name='PLS-1080 Project',
            key='PLS1080',
            org_id=1,
        )
        # Engineering roster in the task org so the org guard accepts a real
        # engineering member as assignee after the resolver runs.
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            organization_slug='shizuha',
            defaults={'tiers': [['nagi@shizuha.com']], 'is_active': True},
        )
        source = Status.objects.create(
            slug='pls1080_blocked',
            name='PLS-1080 Blocked',
            category='in_progress',
        )
        target = Status.objects.create(
            slug='pls1080_done',
            name='PLS-1080 Done',
            category='done',
        )
        self.workflow = Workflow.objects.create(
            name='PLS-1080 Firing User',
            slug='pls1080-firing-user',
            initial_status=source,
            is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow,
            status=source,
            position=0,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow,
            status=target,
            position=1,
        )
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Approve',
            from_status=source.slug,
            to_status=target.slug,
            position=0,
            post_functions=[{'type': 'assign_team', 'value': 'engineering'}],
        )

    def _item(self):
        # Initial state must pass the org guard: the assignee must be a member
        # of the assignment_group roster. Start the item assigned to nagi
        # (engineering roster member) so creation succeeds; the firing user
        # (sora, governance) is passed separately to execute_post_functions.
        return Item.objects.create(
            title='PLS-1080 firing-user exclusion',
            mode='task',
            project=self.project,
            workflow_name=self.workflow.slug,
            status='pls1080_blocked',
            workflow_status='PLS-1080 Blocked',
            assignment_group='engineering',
            assignee_id=self.engineer.id,
            assignee_email=self.engineer.email,
            created_by_id=self.governance_user.id,
            created_by_email=self.governance_user.email,
        )

    @staticmethod
    def _apply(item, uid, email, update_fields, **_kwargs):
        # Apply the resolved engineering member so the terminal item.save()
        # org guard validates a roster member, not the stale governance owner.
        item.assignee_id = uid
        item.assignee_email = email
        for field in ('assignee_id', 'assignee_email'):
            if field not in update_fields:
                update_fields.append(field)
        return uid, email

    def test_firing_user_not_in_roster_is_hard_excluded(self):
        """A governance user firing assign_team:engineering must be excluded
        from the resolver candidates when not an engineering roster member."""
        item = self._item()

        captured = {}

        def resolve(team, **kwargs):
            captured['team'] = team
            captured['hard_exclude_emails'] = kwargs.get('hard_exclude_emails')
            captured['exclude_email'] = kwargs.get('exclude_email')
            return self.engineer.id, self.engineer.email

        with patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=False,
        ), patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=resolve,
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=self._apply,
        ):
            WorkflowService.execute_post_functions(
                self.transition,
                item,
                user=self.governance_user,
            )

        self.assertEqual(captured['team'], 'engineering')
        self.assertEqual(
            captured['hard_exclude_emails'],
            {self.governance_user.email},
        )
        self.assertEqual(captured['exclude_email'], self.governance_user.email)
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, self.engineer.email)

    def test_firing_user_in_roster_is_not_excluded(self):
        """A firing user who IS a roster member of the destination team stays
        a valid candidate (no hard exclusion)."""
        item = self._item()

        captured = {}

        def resolve(team, **kwargs):
            captured['hard_exclude_emails'] = kwargs.get('hard_exclude_emails')
            return self.engineer.id, self.engineer.email

        with patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=True,
        ), patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=resolve,
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=self._apply,
        ):
            WorkflowService.execute_post_functions(
                self.transition,
                item,
                user=self.governance_user,
            )

        self.assertIsNone(captured['hard_exclude_emails'])
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, self.engineer.email)

    def test_no_firing_user_passes_no_exclusion(self):
        """A system/automated transition (user=None) passes no firing-user
        exclusion — behavior unchanged for non-interactive routes."""
        item = self._item()

        captured = {}

        def resolve(team, **kwargs):
            captured['hard_exclude_emails'] = kwargs.get('hard_exclude_emails')
            return self.engineer.id, self.engineer.email

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=resolve,
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=self._apply,
        ):
            WorkflowService.execute_post_functions(
                self.transition,
                item,
                user=None,
            )

        self.assertIsNone(captured['hard_exclude_emails'])
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, self.engineer.email)

    def test_no_candidate_clears_stale_non_roster_owner(self):
        """When assign_team resolves NO candidate (all seats at WIP cap / not
        live) on a group-less item, a stale non-roster assignee (the governance
        user who fired Approve) must be cleared — not carried onto the
        destination group where the exact-org Item.save guard would reject it
        (not-in-team), leaving the Approve one step from broken."""
        item = Item.objects.create(
            title='PLS-1080 stale non-roster owner',
            mode='task',
            project=self.project,
            workflow_name=self.workflow.slug,
            status='pls1080_blocked',
            workflow_status='PLS-1080 Blocked',
            assignment_group=None,
            assignee_id=self.governance_user.id,
            assignee_email=self.governance_user.email,
            created_by_id=self.governance_user.id,
            created_by_email=self.governance_user.email,
        )

        with patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=False,
        ), patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(None, None),
        ):
            WorkflowService.execute_post_functions(
                self.transition,
                item,
                user=self.governance_user,
            )

        item.refresh_from_db()
        # The stale non-roster owner must be cleared; the item is left
        # group-queued on the destination team (org guard passes).
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertIsNone(item.assignee_id)
        self.assertIsNone(item.assignee_email)


class AssignTeamFiringUserArmedBindingTests(TestCase):
    """PLAT-7235: on the armed-binding path _is_team_member is permissive
    (membership = binding candidate) while assignment_org_mismatch is
    fail-closed ('routing-authority'). The firing-user exclusion must use the
    SAME authority: exclude the firing user unless subject_authorized_for_item
    admits them — otherwise the resolver can name a candidate the save fence
    rejects with a confusing routing-authority error."""

    def setUp(self):
        users = get_user_model()
        self.governance_user = users.objects.create_user(
            username='plat7235-gov',
            email='sora@shizuha.com',
            password='x',
        )
        self.engineer = users.objects.create_user(
            username='plat7235-eng',
            email='nagi@shizuha.com',
            password='x',
        )
        self.project = Project.objects.create(
            name='PLAT-7235 Project',
            key='PLAT7235',
            org_id=1,
        )
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            organization_slug='shizuha',
            defaults={'tiers': [['nagi@shizuha.com']], 'is_active': True},
        )
        source = Status.objects.create(
            slug='plat7235_blocked',
            name='PLAT-7235 Blocked',
            category='in_progress',
        )
        target = Status.objects.create(
            slug='plat7235_done',
            name='PLAT-7235 Done',
            category='done',
        )
        self.workflow = Workflow.objects.create(
            name='PLAT-7235 Firing User',
            slug='plat7235-firing-user',
            initial_status=source,
            is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow, status=source, position=0,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow, status=target, position=1,
        )
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Approve',
            from_status=source.slug,
            to_status=target.slug,
            position=0,
            post_functions=[{'type': 'assign_team', 'value': 'engineering'}],
        )

    def _item(self):
        return Item.objects.create(
            title='PLAT-7235 firing-user armed-binding exclusion',
            mode='task',
            project=self.project,
            workflow_name=self.workflow.slug,
            status='plat7235_blocked',
            workflow_status='PLAT-7235 Blocked',
            assignment_group='engineering',
            assignee_id=self.engineer.id,
            assignee_email=self.engineer.email,
            created_by_id=self.governance_user.id,
            created_by_email=self.governance_user.email,
        )

    @staticmethod
    def _apply(item, uid, email, update_fields, **_kwargs):
        item.assignee_id = uid
        item.assignee_email = email
        for field in ('assignee_id', 'assignee_email'):
            if field not in update_fields:
                update_fields.append(field)
        return uid, email

    def _run(self, item, resolve_side_effect=None, authorized_allowed=False):
        from types import SimpleNamespace
        captured = {}

        def resolve(team, **kwargs):
            captured['team'] = team
            captured['hard_exclude_emails'] = kwargs.get('hard_exclude_emails')
            return (self.engineer.id, self.engineer.email) \
                if resolve_side_effect is None else resolve_side_effect(team, **kwargs)

        with patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=True,  # permissive armed-binding path
        ), patch(
            'tasks.services.routing_authority.binding_candidates_for_item',
            return_value={'candidates': [
                {'user_id': self.governance_user.id,
                 'email': self.governance_user.email},
            ]},
        ), patch(
            'tasks.services.routing_authority.subject_authorized_for_item',
            return_value=SimpleNamespace(
                allowed=authorized_allowed,
                reason=None if authorized_allowed else 'routing-authority',
            ),
        ) as authorized, patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=resolve,
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=self._apply,
        ):
            WorkflowService.execute_post_functions(
                self.transition,
                item,
                user=self.governance_user,
            )
        return captured, authorized

    def test_binding_candidate_firing_user_denied_by_authority_is_excluded(self):
        """A firing user who is a binding candidate (permissive membership) but
        NOT subject_authorized_for_item-admitted must be hard-excluded — the
        route must never name a tuple the save fence rejects."""
        item = self._item()
        captured, authorized = self._run(item)

        authorized.assert_called_once()
        self.assertEqual(authorized.call_args.kwargs.get('team_name'), 'engineering')
        self.assertEqual(
            captured['hard_exclude_emails'],
            {self.governance_user.email},
        )
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, self.engineer.email)

    def test_binding_candidate_firing_user_admitted_stays_candidate(self):
        """A firing user the routing authority admits stays a valid candidate
        (no hard exclusion) — the exclusion must not over-reach."""
        from types import SimpleNamespace
        item = self._item()
        captured2 = {}

        def resolve2(team, **kwargs):
            captured2['hard_exclude_emails'] = kwargs.get('hard_exclude_emails')
            return self.engineer.id, self.engineer.email

        with patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=True,
        ), patch(
            'tasks.services.routing_authority.binding_candidates_for_item',
            return_value={'candidates': [
                {'user_id': self.governance_user.id,
                 'email': self.governance_user.email},
            ]},
        ), patch(
            'tasks.services.routing_authority.subject_authorized_for_item',
            return_value=SimpleNamespace(allowed=True, reason=None),
        ), patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=resolve2,
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=self._apply,
        ):
            WorkflowService.execute_post_functions(
                self.transition,
                item,
                user=self.governance_user,
            )

        self.assertIsNone(captured2['hard_exclude_emails'])
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, self.engineer.email)

    def test_binding_lookup_failure_fails_open(self):
        """A routing-authority lookup error must not wedge the route — the
        exclusion fails open and the org guard still enforces at save."""
        item = self._item()
        captured = {}

        def resolve(team, **kwargs):
            captured['hard_exclude_emails'] = kwargs.get('hard_exclude_emails')
            return self.engineer.id, self.engineer.email

        with patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=True,
        ), patch(
            'tasks.services.routing_authority.binding_candidates_for_item',
            side_effect=RuntimeError('authority dark'),
        ), patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=resolve,
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=self._apply,
        ):
            WorkflowService.execute_post_functions(
                self.transition,
                item,
                user=self.governance_user,
            )

        # Fail open: no exclusion fired despite the permissive membership.
        self.assertIsNone(captured['hard_exclude_emails'])
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, self.engineer.email)
