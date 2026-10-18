"""PLAT-11229 regression: roster reconciliation honors person-made direct
assignments (both polarities) and does NOT drop agent MCP assignments that
stamp user_id 0 with a real agent email (reika review follow-up).

Drives the real `_locked_roster_repair` honor path through
`_sweep_rebalance_pods_impl` (the roster-membership reconciliation pass) with
the sweep's externals patched to deterministic no-ops. The discriminator under
test is the item's most recent ``assigned`` ItemActivity boundary actor.
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from tasks.models import (
    Item, ItemActivity, Project, ReceiveAssignmentEntitlement,
    RoutingAuthorityCheckpoint, RoutingSubject, RoutingTeam, RoutingTeamBinding,
    TeamRoster,
)


def _patch_sweep_externals():
    """Patch every external the roster-reconciliation pass consults so the test
    isolates the PLAT-11229 honor branch (both polarities) and the id-0 edge."""
    from tasks.services import workflow_service as ws_module

    return [
        # Arm the exact routing lane so the materialized RoutingTeam serves
        # binding candidates (recipe mirror, test_plat4213_org_guard:623):
        # a materialized lane with the gate dark means HOLD, not fallback.
        patch.dict('os.environ', {
            'PULSE_ROUTING_BINDINGS_ENFORCE': '1',
            'PULSE_ROUTING_BINDINGS_TEAMS': 'engineering',
        }),
        patch('tasks.tasks._rebalance_pulse_api_health_gate', return_value=(True, None)),
        # The current owner is deliberately NOT on the row's team roster.
        patch.object(ws_module.WorkflowService, '_is_team_member', return_value=False),
        patch.object(ws_module.WorkflowService, '_is_human_account', return_value=False),
        patch.object(ws_module.WorkflowService, '_is_operator_target', return_value=False),
        patch.object(ws_module.WorkflowService, '_item_failover_actionable', return_value=True),
        patch.object(ws_module.WorkflowService, '_active_blockers_for', return_value=[]),
        # No eligible replacement resolvable → legacy polarity exercises the
        # clear-to-group-queue path (PLS-408 reconciliation behavior).
        patch.object(
            ws_module.WorkflowService, '_resolve_team_assignee',
            return_value=(None, ''),
        ),
    ]


class RosterReconciliationHonorPathTests(TestCase):
    def setUp(self):
        # PLAT-11229: arm the exact routing lane's env gates BEFORE setUp
        # staffing. The fixture materializes the RoutingTeam aggregate (the
        # PLS-539 recipe), and a materialized lane with the gate dark serves
        # HOLD rather than legacy TeamRoster fallback (rollback invariant
        # §16.9, routing_authority.py:940) — the un-armed lane refused the
        # setUp assignment itself and cleared the seat before the test body.
        self._gate_patches = [
            patch.dict('os.environ', {
                'PULSE_ROUTING_BINDINGS_ENFORCE': '1',
                'PULSE_ROUTING_BINDINGS_TEAMS': 'engineering',
            }),
        ]
        for _p in self._gate_patches:
            _p.start()
            self.addCleanup(_p.stop)
        self.project = Project.objects.create(
            key='PLAT', org_id=1, name='Shizuha Platform',
            access_level='open', is_active=True)
        self.off_roster_email = 'recon@agents.shizuha.io'
        self.item = Item.objects.create(
            mode='task',
            title='PLAT-11229 honor-path regression fixture',
            project=self.project,
            status='open',
            assignment_group='engineering',
            priority='high',
            story_points=3,
        )
        # Seed the guarded 'engineering' routing lane the Item.save assignee
        # authorization consults (mirror of the PLS-539 materialized-lane
        # recipe): team + active subject for the owner + checkpoint + binding +
        # one entitlement for this item's workflow. Assignee is stamped AFTER
        # the lane exists so the save-time roster validator passes.
        team = RoutingTeam.objects.create(
            organization_id=1, key='engineering', display_name='Engineering',
            state='active', version=1, digest='1' * 64)
        now = timezone.now()
        subject = RoutingSubject.objects.create(
            organization_id=1, subject_id=4829,
            canonical_email=self.off_roster_email, state='active',
            generation=1, digest='2' * 64, observed_at=now,
            lease_until=now + timedelta(seconds=90))
        RoutingAuthorityCheckpoint.objects.create(
            authority_id='shizuha-id', aggregate_key='1:4829',
            subject_generation=1, observed_version=1, digest=subject.digest,
            revocation_high_water=0, checkpoint_generation=1,
            source_event_id=uuid.uuid4(), first_received_at=now,
            source_expires_at=now + timedelta(seconds=90),
            observed_at=now, lease_until=now + timedelta(seconds=90))
        RoutingTeamBinding.objects.create(
            organization_id=1, routing_team=team, subject=subject,
            state='active', version=1, is_current=True, subject_generation=1,
            command_id=uuid.uuid4(), digest='3' * 64)
        ReceiveAssignmentEntitlement.objects.create(
            organization_id=1, subject=subject, routing_team=team,
            project=None, workflow_name=self.item.workflow_name,
            action='receive_assignment', state='active', version=1,
            is_current=True, subject_generation=1,
            grant_source='reviewed_task:PLAT-11229', grant_issuer_id=77,
            grant_issuer_email='ren@shizuha.com', grant_event_id=uuid.uuid4(),
            command_id=uuid.uuid4(), digest='4' * 64)
        self.item.assignee_email = self.off_roster_email
        # PLS-278 assignee-write-guard: the routability guard (enforced by
        # default) rejects any assignee write whose team roster has zero
        # members — trusted run 8989's clear-leg evidence named exactly this
        # (`assignee-write-guard:engineering:team_unstaffed`, held the item
        # group-queued). Seed the DB-backed roster the guard consults so the
        # lane is truthfully staffed, matching the guarded production lane.
        # Roster rows carry the canonical @shizuha.com identity (trusted run
        # 8991: the tiers validator refuses agents.shizuha.io addresses);
        # the inbound agents.shizuha.io assignee resolves to the roster
        # member through the agent-email alias plane.
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={'organization_id': 1, 'tiers': [['recon@shizuha.com']],
                      'is_active': True})
        # The resolver-sanctioned write path: _apply_resolved_assignee stamps
        # the typed routing-authority decision receipt that the guarded
        # Item.save fence consumes (raw kwarg saves die with direct_writer_denied
        # — the 2026-10-04 incident machinery). This is the truthful write lane,
        # not a bypass.
        fields = []
        from tasks.services import workflow_service as ws_module
        WorkflowService = ws_module.WorkflowService
        WorkflowService._apply_resolved_assignee(
            self.item, 4829, self.off_roster_email, fields,
        )
        self.item.save(update_fields=['assignee_id', 'assignee_email'])

    def _stamp_assigned_boundary(self, user_email, user_id):
        ItemActivity.objects.create(
            item=self.item,
            action='assigned',
            field_name='assignee_email',
            old_value='',
            new_value=self.off_roster_email,
            user_id=user_id,
            user_email=user_email,
            # Strictly later than the setUp landing's fence receipt so the
            # most-recent-boundary ordering is deterministic (a microsecond
            # tie would let -created_at ordering return the landing actor).
            created_at=timezone.now() + timezone.timedelta(seconds=1),
        )

    def _run_sweep(self):
        import contextlib

        from tasks.tasks import _sweep_rebalance_pods_impl
        with contextlib.ExitStack() as stack:
            for p in _patch_sweep_externals():
                stack.enter_context(p)
            return _sweep_rebalance_pods_impl()

    def test_named_actor_boundary_keeps_off_roster_direct_owner(self):
        """Honor polarity: a person-made (named actor) assigned boundary keeps
        the off-roster concrete owner and stamps the audit metadata once."""
        self._stamp_assigned_boundary('goro@agents.shizuha.io', 8)
        self._run_sweep()
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_email, self.off_roster_email)
        self.assertIsNotNone((self.item.metadata or {}).get('direct_override_honored_at'))

    def test_system_actor_boundary_still_clears_to_group_queue(self):
        """Legacy polarity: a system@pulse (user_id 0) assigned boundary keeps
        the pre-existing reconciliation behavior — the ineligible seat is
        cleared to the group queue."""
        self._stamp_assigned_boundary('system@pulse', 0)
        self._run_sweep()
        self.item.refresh_from_db()
        self.assertNotEqual(self.item.assignee_email, self.off_roster_email)
        self.assertEqual((self.item.metadata or {}).get('direct_override_honored_at', None), None)

    def test_scheduler_actor_boundary_still_clears(self):
        """Scheduler machinery also stamps user_id 0 with a machine identity —
        system actor, legacy reconciliation behavior preserved."""
        self._stamp_assigned_boundary('scheduler@pulse.shizuha.com', 0)
        self._run_sweep()
        self.item.refresh_from_db()
        self.assertNotEqual(self.item.assignee_email, self.off_roster_email)

    def _clear_leg_evidence(self):
        """The sweep posts a system comment naming the leg whenever it clears
        an owner (roster PLS-408, linked-blocked PLS-524, down-owner drain).
        Surface that trail on assertion failure so one CI lap diagnoses the
        discriminator instead of N."""
        from .models import Comment, ItemActivity
        comments = list(
            Comment.objects.filter(item=self.item)
            .order_by('-created_at')
            .values_list('author_email', 'content')[:3]
        )
        boundaries = list(
            ItemActivity.objects.filter(item=self.item, action='assigned')
            .order_by('-created_at')
            .values_list('user_email', 'user_id', 'created_at')[:3]
        )
        return (
            f'clear-leg evidence — boundaries={boundaries!r} '
            f'comments={comments!r} '
            f'metadata={self.item.metadata!r}'
        )

    def test_agent_mcp_assignment_with_zero_user_id_and_real_email_is_honored(self):
        """Edge (2026-10-04 incident class): agent MCP assignments stamp
        user_id 0 with a real agent email (ID-service user not resolvable from
        the writing seat). The blanket id==0 reject must NOT clear these — the
        boundary is a person-made direct assignment."""
        self._stamp_assigned_boundary(self.off_roster_email, 0)
        self._run_sweep()
        self.item.refresh_from_db()
        self.assertEqual(
            self.item.assignee_email,
            self.off_roster_email,
            msg=self._clear_leg_evidence(),
        )
        self.assertIsNotNone((self.item.metadata or {}).get('direct_override_honored_at'))
