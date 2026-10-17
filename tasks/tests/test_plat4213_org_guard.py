"""PLAT-4213/PLAT-4572 exact-org roster and routing-authority guards."""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import ast
import os
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.color import no_style
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from tasks.models import (
    Comment, CrossOrgAssignmentDelegation, Item, Project, TeamRoster,
    ReceiveAssignmentEntitlement,
    RoutingAuthorityCheckpoint, RoutingAuthorityCommand,
    RoutingAuthorityConsumerState, RoutingAuthorityHoldSignal,
    RoutingAuthorityOutbox, RoutingAuthorityQuarantine, RoutingAssignmentReceipt,
    RoutingSubject, RoutingTeam, RoutingTeamBinding,
)
from tasks.permissions import is_item_participant, is_item_team_member
from tasks.services.routing_authority import (
    RoutingAuthorityError, apply_consumer_event, approve_and_apply_command,
    apply_authority_checkpoint, binding_candidates_for_item,
    claim_freshness_reconciles, propose_command, refresh_identity_checkpoints,
    render_routing_authority_metrics, reroute_assignment_to_group_queue,
    routing_candidate_snapshot_for_item, subject_authorized_for_item,
)
from tasks.tasks import (
    _org_health_candidate_availability, relay_routing_authority_outbox,
    sweep_assign_group_queued,
)
from tasks.services.workflow_service import WorkflowService


User = get_user_model()


class RosterOrgTests(TestCase):
    def setUp(self):
        TeamRoster.objects.create(
            team_name='digital-accounting', tiers=[['kane@shizuha.com']],
            organization_slug='shizuha-digital', is_active=True)
        TeamRoster.objects.create(
            team_name='accounting', tiers=[['banto@shizuha.com']],
            organization_slug='shizuha', is_active=True)

    def test_roster_org_for_email(self):
        self.assertEqual(WorkflowService.roster_org_for_email('kane@shizuha.com'), 'shizuha-digital')
        self.assertEqual(WorkflowService.roster_org_for_email('banto@shizuha.com'), 'shizuha')
        self.assertEqual(WorkflowService.roster_org_for_email('nobody@x.com'), '')

    def test_migration_default_backfills_shizuha(self):
        r = TeamRoster.objects.create(team_name='newteam', tiers=[[]])
        self.assertEqual(r.organization_slug, 'shizuha')

    def test_database_team_key_canonicalizes_platform_ops(self):
        if connection.vendor != 'postgresql':
            self.skipTest('PostgreSQL assignment guard contract')
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tasks_canonical_team_key('platform-ops')",
            )
            canonical = cursor.fetchone()[0]
        self.assertEqual(canonical, 'devops')


class PhantomTeamCoordinationFallbackTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Books',
            key='BKDF',
            org_id=2,
            access_level='open',
        )
        self.kane = User.objects.create_user(
            username='kane-fallback',
            email='kane-fallback@shizuha.com',
            password='x',
        )
        TeamRoster.objects.update_or_create(
            team_name='accounting',
            organization_slug='shizuha-digital',
            defaults={
                'organization_id': 2,
                'tiers': [['kane-fallback@shizuha.com']],
                'is_active': True,
            },
        )
        # A same-named team in another organization must never satisfy the task.
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            organization_slug='shizuha',
            defaults={
                'organization_id': 1,
                'tiers': [['haru@shizuha.com']],
                'is_active': True,
            },
        )
        self.item = Item.objects.create(
            mode='task',
            item_type='task.bug',
            title='Coordinate tenant-local framework defect',
            project=self.project,
            item_key='BKDF-1',
            assignment_group='engineering',
            status='open',
            created_by_id=self.kane.id,
            created_by_email=self.kane.email,
            tags='team-resolver',
        )

    @patch.object(
        WorkflowService,
        '_is_agent_available_for_assignment',
        return_value=True,
    )
    @patch.object(
        WorkflowService,
        '_member_can_execute_from_status',
        return_value=True,
    )
    @patch.object(WorkflowService, '_agent_active_load', return_value=0)
    @patch.object(WorkflowService, '_agent_workable_load', return_value=0)
    @patch('tasks.user_service.user_service.get_user_by_email')
    def test_phantom_team_returns_to_unique_creator_team_then_assigns(
        self,
        get_user,
        _workable,
        _active,
        _can_execute,
        _available,
    ):
        get_user.return_value = {
            'id': self.kane.id,
            'email': self.kane.email,
            'username': self.kane.username,
        }

        result = sweep_assign_group_queued.run(
            only_agent_email=self.kane.email,
        )

        self.item.refresh_from_db()
        self.assertEqual(result['rerouted'], 1)
        self.assertEqual(result['assigned'], 1)
        self.assertEqual(self.item.assignment_group, 'accounting')
        self.assertEqual(self.item.assignee_id, self.kane.id)
        self.assertEqual(self.item.assignee_email, self.kane.email)
        self.assertIn(
            'org-team-coordination-fallback',
            self.item.tags.split(','),
        )
        self.assertEqual(
            self.item.metadata['org_team_coordination_fallback']['from_team'],
            'engineering',
        )
        self.assertEqual(
            self.item.metadata['org_team_coordination_fallback']['to_team'],
            'accounting',
        )
        self.assertTrue(Comment.objects.filter(
            item=self.item,
            author_email='system@pulse',
            content__contains='no cross-organization team or agent was borrowed',
        ).exists())

    def test_missing_creator_membership_remains_honestly_queued(self):
        TeamRoster.objects.filter(
            team_name='accounting',
            organization_id=2,
        ).update(tiers=[['other-accountant@shizuha.com']])

        result = sweep_assign_group_queued.run(
            only_agent_email=self.kane.email,
        )

        self.item.refresh_from_db()
        self.assertEqual(result['rerouted'], 0)
        self.assertEqual(result['assigned'], 0)
        self.assertEqual(self.item.assignment_group, 'engineering')
        self.assertFalse(self.item.assignee_email)

    def test_existing_exact_org_team_is_never_replaced(self):
        TeamRoster.objects.update_or_create(
            team_name='engineering',
            organization_slug='shizuha-digital',
            defaults={
                'organization_id': 2,
                'tiers': [['digital-engineer@shizuha.com']],
                'is_active': True,
            },
        )

        result = sweep_assign_group_queued.run(
            only_agent_email=self.kane.email,
        )

        self.item.refresh_from_db()
        self.assertEqual(result['rerouted'], 0)
        self.assertEqual(result['assigned'], 0)
        self.assertEqual(self.item.assignment_group, 'engineering')

    @patch.object(
        WorkflowService,
        '_is_agent_available_for_assignment',
        return_value=True,
    )
    @patch.object(
        WorkflowService,
        '_member_can_execute_from_status',
        return_value=True,
    )
    @patch.object(WorkflowService, '_agent_active_load', return_value=0)
    @patch.object(WorkflowService, '_agent_workable_load', return_value=0)
    @patch('tasks.user_service.user_service.get_user_by_email')
    def test_review_lane_restores_recorded_exact_org_fallback(
        self,
        get_user,
        _workable,
        _active,
        _can_execute,
        _available,
    ):
        get_user.return_value = {
            'id': self.kane.id,
            'email': self.kane.email,
            'username': self.kane.username,
        }
        self.item.status = 'in_review'
        self.item.workflow_name = 'autonomous-bug'
        self.item.assignment_group = 'qa'
        self.item.metadata = {
            'org_team_coordination_fallback': {
                'version': 1,
                'organization_id': 2,
                'from_team': 'engineering',
                'to_team': 'accounting',
                'creator_email': self.kane.email,
                'reason': 'task_team_has_no_exact_org_roster',
            },
        }
        self.item.tags = 'team-resolver,org-team-coordination-fallback'
        self.item.save(update_fields=[
            'status', 'workflow_name', 'assignment_group', 'metadata', 'tags',
            'updated_at',
        ])

        result = sweep_assign_group_queued.run(
            only_agent_email=self.kane.email,
        )

        self.item.refresh_from_db()
        self.assertEqual(result['rerouted'], 1)
        self.assertEqual(result['assigned'], 1)
        self.assertEqual(self.item.status, 'in_review')
        self.assertEqual(self.item.assignment_group, 'accounting')
        self.assertEqual(self.item.assignee_email, self.kane.email)
        self.assertTrue(Comment.objects.filter(
            item=self.item,
            author_email='system@pulse',
            content__contains='Restored the existing audited tenant-local',
        ).exists())

    def test_real_exact_org_review_lane_wins_over_recorded_fallback(self):
        TeamRoster.objects.create(
            team_name='qa',
            organization_slug='shizuha-digital',
            organization_id=2,
            tiers=[['digital-qa@shizuha.com']],
            is_active=True,
        )
        self.item.status = 'in_review'
        self.item.workflow_name = 'autonomous-bug'
        self.item.assignment_group = 'qa'
        self.item.metadata = {
            'org_team_coordination_fallback': {
                'version': 1,
                'organization_id': 2,
                'from_team': 'engineering',
                'to_team': 'accounting',
                'creator_email': self.kane.email,
                'reason': 'task_team_has_no_exact_org_roster',
            },
        }
        self.item.save(update_fields=[
            'status', 'workflow_name', 'assignment_group', 'metadata',
            'updated_at',
        ])

        result = sweep_assign_group_queued.run(
            only_agent_email=self.kane.email,
        )

        self.item.refresh_from_db()
        self.assertEqual(result['rerouted'], 0)
        self.assertEqual(self.item.assignment_group, 'qa')


class CrossOrgAssignGuardTests(TestCase):
    def setUp(self):
        TeamRoster.objects.create(
            team_name='digital-accounting', tiers=[['kane@shizuha.com']],
            organization_slug='shizuha-digital', is_active=True)
        self.proj_digital = Project.objects.create(name='BKSD', key='BKSD', org_id=2, access_level='open')
        self.proj_global = Project.objects.create(name='PLAT', key='PLATX', org_id=1, access_level='open')

    def _orgmap(self, org_id):
        return {1: {'id': 1, 'slug': 'shizuha'},
                2: {'id': 2, 'slug': 'shizuha-digital'},
                3: {'id': 3, 'slug': 'shizuha-trading'},
                4: {'id': 4, 'slug': 'personal-3'}}.get(org_id)

    def test_same_org_allowed_other_org_blocked(self):
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            self.assertEqual(WorkflowService.org_slug_for_project(self.proj_digital), 'shizuha-digital')
            self.assertEqual(WorkflowService.org_slug_for_project(self.proj_global), 'shizuha')
        # guard logic composition (mirrors views assign guard):
        roster_org = WorkflowService.roster_org_for_email('kane@shizuha.com')
        self.assertEqual(roster_org, 'shizuha-digital')
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            task_org = WorkflowService.org_slug_for_project(self.proj_global)
        self.assertTrue(roster_org and task_org and roster_org != task_org)

    def test_same_team_name_is_distinct_per_organization(self):
        TeamRoster.objects.create(
            team_name='accounting', tiers=[['global-accountant@shizuha.com']],
            organization_slug='shizuha', is_active=True,
        )
        TeamRoster.objects.update_or_create(
            team_name='accounting', organization_slug='shizuha-digital',
            defaults={'tiers': [['digital-accountant@shizuha.com']], 'is_active': True},
        )
        self.assertEqual(TeamRoster.objects.filter(team_name='accounting').count(), 2)

    def test_org_roster_wins_and_business_team_never_uses_foreign_fallback(self):
        # ``accounting`` is a multi-org business lane in production (shizuha,
        # shizuha-digital, shizuha-trading, personal-3), so it is NOT
        # shizuha-only — the PLS-1069 cell-C fallback must never borrow it for
        # a foreign task (PLAT-4213 no-foreign-borrowing guard).
        TeamRoster.objects.create(
            team_name='accounting', tiers=[['global-accountant@shizuha.com']],
            organization_slug='shizuha', is_active=True,
        )
        TeamRoster.objects.update_or_create(
            team_name='accounting', organization_slug='shizuha-digital',
            defaults={'tiers': [['digital-accountant@shizuha.com']], 'is_active': True},
        )
        TeamRoster.objects.update_or_create(
            team_name='accounting', organization_slug='shizuha-trading',
            defaults={'tiers': [['trading-accountant@shizuha.com']], 'is_active': True},
        )
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            tiers, org = WorkflowService._team_roster_entry_for_item(
                'accounting', Item(project=self.proj_digital),
            )
        self.assertEqual(org, 'shizuha-digital')
        self.assertEqual(tiers, [['digital-accountant@shizuha.com']])

        TeamRoster.objects.filter(
            team_name='accounting', organization_slug='shizuha-digital').delete()
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            tiers, org = WorkflowService._team_roster_entry_for_item(
                'accounting', Item(project=self.proj_digital),
            )
        # No org-local row for shizuha-digital, and ``accounting`` is NOT
        # shizuha-only (it still has a shizuha-trading row), so the cell-C
        # fallback must NOT apply — the team stays unresolvable for this task.
        self.assertIsNone(tiers)
        self.assertEqual(org, '')

    def test_shizuha_only_business_team_never_falls_back(self):
        """PLS-1069 cell-C gate (sora review): a BUSINESS team that happens to
        be shizuha-only (no row in any non-shizuha org) must NOT fall back to
        the platform roster for a foreign task. The cell-C fallback is reserved
        for platform routing/escalation lanes (``_PLATFORM_LANES``); a
        shizuha-only ``accounting`` would otherwise authorize a shizuha-digital
        task via the shizuha row — the PLAT-4213 no-foreign-borrowing guard
        must hold even when the lane has no non-shizuha counterpart."""
        # ``accounting`` is a business lane with ONLY a shizuha row here — a
        # genuinely shizuha-only state that must still never borrow cross-org.
        TeamRoster.objects.update_or_create(
            team_name='accounting', organization_slug='shizuha',
            defaults={'tiers': [['global-accountant@shizuha.com']], 'is_active': True},
        )
        TeamRoster.objects.filter(
            team_name='accounting',
        ).exclude(organization_slug='shizuha').delete()
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            tiers, org = WorkflowService._team_roster_entry_for_item(
                'accounting', Item(project=self.proj_digital),
            )
        # No org-local row AND the team is shizuha-only, but it is not a
        # platform lane -> the fallback must NOT apply.
        self.assertIsNone(tiers)
        self.assertEqual(org, '')

    def test_platform_team_does_not_fall_back_to_shizuha_roster(self):
        # ``review`` is a multi-org lane (also exists in shizuha-trading), so
        # it is NOT shizuha-only — the cell-C fallback must not borrow the
        # shizuha ``review`` row for a shizuha-digital task.
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha',
            defaults={'tiers': [['reviewer@shizuha.com']], 'is_active': True},
        )
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha-trading',
            defaults={'tiers': [['trading-reviewer@shizuha.com']], 'is_active': True},
        )
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            tiers, org = WorkflowService._team_roster_entry_for_item(
                'review', Item(project=self.proj_digital),
            )
        self.assertEqual(org, '')
        self.assertIsNone(tiers)

    def test_cell_c_shizuha_only_team_registered_via_platform_fallback(self):
        """PLS-731 cell C: a slug with NO org-local row but a shizuha row is
        still routable from a non-shizuha task (the majority case: 20 of 22
        platform slugs have no non-shizuha counterpart)."""
        TeamRoster.objects.update_or_create(
            team_name='human-ops', organization_slug='shizuha',
            defaults={'tiers': [['cos@shizuha.com']], 'is_active': True},
        )
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            registered = WorkflowService._team_registered_for_item(
                'human-ops', Item(project=self.proj_digital),
            )
        self.assertTrue(registered)

    def test_cell_b_colliding_slug_resolves_org_local_not_platform(self):
        """PLS-731 cell B: a slug present in BOTH the task org and shizuha
        resolves org-locally; the platform row is never borrowed for a
        colliding slug."""
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha',
            defaults={'tiers': [['reviewer@shizuha.com']], 'is_active': True},
        )
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha-digital',
            defaults={'tiers': [['digital-reviewer@shizuha.com']], 'is_active': True},
        )
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            registered = WorkflowService._team_registered_for_item(
                'review', Item(project=self.proj_digital),
            )
            tiers, org = WorkflowService._team_roster_entry_for_item(
                'review', Item(project=self.proj_digital),
            )
        self.assertTrue(registered)
        self.assertEqual(org, 'shizuha-digital')
        self.assertEqual(tiers, [['digital-reviewer@shizuha.com']])

    def test_unknown_team_still_rejected_after_platform_fallback(self):
        """A slug with no row in the task org AND no shizuha row remains
        unregistered — the cell-C fallback must not invent teams."""
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            registered = WorkflowService._team_registered_for_item(
                'cortex', Item(project=self.proj_digital),
            )
        self.assertFalse(registered)

    def test_assignment_guard_keeps_business_teams_org_scoped_in_review(self):
        # ``accounting``/``review`` are multi-org business lanes (also exist in
        # shizuha-trading), so they are NOT shizuha-only — the PLS-1069 cell-C
        # fallback must not borrow the shizuha row for a shizuha-digital task.
        TeamRoster.objects.create(
            team_name='accounting', tiers=[['global-accountant@shizuha.com']],
            organization_slug='shizuha', is_active=True,
        )
        TeamRoster.objects.update_or_create(
            team_name='accounting', organization_slug='shizuha-trading',
            defaults={'tiers': [['trading-accountant@shizuha.com']], 'is_active': True},
        )
        TeamRoster.objects.update_or_create(
            team_name='review',
            organization_slug='shizuha',
            defaults={
                'tiers': [['global-accountant@shizuha.com']],
                'is_active': True,
            },
        )
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha-trading',
            defaults={'tiers': [['trading-reviewer@shizuha.com']], 'is_active': True},
        )
        item = Item(
            project=self.proj_digital, status='in_review',
            assignment_group='accounting',
        )
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            mismatch, roster_org, task_org = WorkflowService.assignment_org_mismatch(
                item, 'global-accountant@shizuha.com', 'accounting',
            )
            platform_mismatch = WorkflowService.assignment_org_mismatch(
                item, 'global-accountant@shizuha.com', 'review',
            )[0]
        self.assertTrue(mismatch)
        # PLS-1080: the truthful sentinel (not-in-team) is returned instead of
        # the assignee's own org masking the real reason (the old tuple was
        # ('shizuha', 'shizuha-digital') — misleading, since the assignee IS a
        # shizuha member, just not in the shizuha-digital accounting team).
        self.assertEqual((roster_org, task_org), ('not-in-team', 'shizuha-digital'))
        self.assertTrue(platform_mismatch)

    def test_active_candidate_membership_uses_the_item_org(self):
        TeamRoster.objects.create(
            team_name='accounting', tiers=[['global-accountant@shizuha.com']],
            organization_slug='shizuha', is_active=True,
        )
        TeamRoster.objects.update_or_create(
            team_name='accounting', organization_slug='shizuha-digital',
            defaults={'tiers': [['digital-accountant@shizuha.com']], 'is_active': True},
        )
        item = Item(project=self.proj_digital)
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          return_value=True):
            candidates = WorkflowService._active_team_candidate_emails(
                'accounting', item=item,
            )
        self.assertEqual(candidates, {'digital-accountant@shizuha.com'})

    def test_recurring_owner_repair_rechecks_org_even_when_group_is_unchanged(self):
        TeamRoster.objects.create(
            team_name='accounting', tiers=[['global-accountant@shizuha.com']],
            organization_slug='shizuha', is_active=True,
        )
        TeamRoster.objects.update_or_create(
            team_name='accounting', organization_slug='shizuha-digital',
            defaults={'tiers': [['digital-accountant@shizuha.com']], 'is_active': True},
        )
        item = Item(
            project=self.proj_digital, status='open', is_recurring=True,
            assignment_group='accounting', assignee_id=10,
            assignee_email='global-accountant@shizuha.com',
            metadata={'recurring_owner_team': 'accounting'},
        )
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap), \
             patch.object(WorkflowService, '_resolve_team_assignee',
                          return_value=(11, 'digital-accountant@shizuha.com')):
            changed = WorkflowService._restore_recurring_owner_team(item)
        self.assertTrue(changed)
        self.assertEqual(item.assignee_email, 'digital-accountant@shizuha.com')


class CrossOrgTeamCommentAccessTests(APITestCase):
    """A same-named foreign roster must never grant private-task comments."""

    def setUp(self):
        self.client = APIClient()
        self.project = Project.objects.create(
            name='Digital Books', key='DBKS', org_id=2,
            access_level='private', is_active=True,
        )
        self.item = Item.objects.create(
            mode='task', title='Reconcile ledger', project=self.project,
            assignment_group='accounting',
        )
        self.digital_member = User.objects.create_user(
            username='digital-accountant', email='digital-accountant@shizuha.com',
            password='x', is_staff=False,
        )
        self.global_member = User.objects.create_user(
            username='global-accountant', email='global-accountant@shizuha.com',
            password='x', is_staff=False,
        )
        TeamRoster.objects.create(
            team_name='accounting', organization_slug='shizuha',
            tiers=[['global-accountant@shizuha.com']], is_active=True,
        )
        TeamRoster.objects.update_or_create(
            team_name='accounting', organization_slug='shizuha-digital',
            defaults={'tiers': [['digital-accountant@shizuha.com']], 'is_active': True},
        )

    @staticmethod
    def _orgmap(org_id):
        return {
            1: {'id': 1, 'slug': 'shizuha'},
            2: {'id': 2, 'slug': 'shizuha-digital'},
        }.get(org_id)

    def _post_comment(self, user):
        self.client.force_authenticate(user)
        return self.client.post(
            '/api/comments/', {'item': self.item.id, 'content': 'handoff'},
            format='json',
        )

    @patch('tasks.services.admin_service.admin_service.get_organization')
    def test_comment_membership_allows_own_org_and_denies_foreign_org(self, get_org):
        get_org.side_effect = self._orgmap

        self.assertTrue(is_item_team_member(self.digital_member, self.item))
        self.assertFalse(is_item_team_member(self.global_member, self.item))
        self.assertEqual(self._post_comment(self.digital_member).status_code, 201)
        self.assertEqual(self._post_comment(self.global_member).status_code, 400)


class PLAT4572RoutingAuthorityTests(APITestCase):
    """ADR-0041 binding-only routing with independent, fresh authorization."""

    def setUp(self):
        self.gate = patch.dict('os.environ', {
            'PULSE_ROUTING_BINDINGS_ENFORCE': '1',
            'PULSE_ROUTING_BINDINGS_TEAMS': 'cortex',
        })
        self.gate.start()
        self.addCleanup(self.gate.stop)
        self.project = Project.objects.create(
            name='SCLI', key='SCLX', org_id=1, access_level='open', is_active=True,
        )
        self.ichi = User.objects.create_user(
            id=88, username='ichi', email='ichi@shizuha.com', password='x',
            is_staff=True,
        )
        # This fixture intentionally models Ichi's stable external subject ID.
        # PostgreSQL doesn't advance a sequence for an explicit primary key, so
        # later create_user() calls can collide at 88 depending on parallel test
        # ordering unless the fixture repairs the sequence immediately.
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), [User]):
                cursor.execute(sql)
        TeamRoster.objects.update_or_create(
            team_name='security', organization_slug='shizuha',
            defaults={
                'tiers': [['ichi@shizuha.com']], 'junior_emails': [],
                'is_active': True, 'note': 'canonical security authority',
                'updated_by': 'fixture',
            },
        )
        self.security_before = list(TeamRoster.objects.filter(
            team_name='security').values(
                'id', 'organization_slug', 'team_name', 'tiers', 'junior_emails',
                'is_active', 'note', 'updated_by'))
        self.item = Item.objects.create(
            mode='task', item_type='task.bug', title='Cortex exact-org route',
            project=self.project, item_key='SCLX-1', assignment_group='cortex', status='open',
        )
        self.team = RoutingTeam.objects.create(
            organization_id=1, key='cortex', display_name='Cortex',
            state='active', version=1, digest='1' * 64,
        )
        now = timezone.now()
        self.subject = RoutingSubject.objects.create(
            organization_id=1, subject_id=88, canonical_email='ichi@shizuha.com',
            state='active', generation=1, digest='2' * 64,
            observed_at=now, lease_until=now + timedelta(seconds=90),
        )
        RoutingAuthorityCheckpoint.objects.create(
            authority_id='shizuha-id', aggregate_key='1:88',
            subject_generation=1, observed_version=1, digest=self.subject.digest,
            revocation_high_water=0, checkpoint_generation=1,
            source_event_id=uuid.uuid4(), first_received_at=now,
            source_expires_at=now + timedelta(seconds=90),
            observed_at=now, lease_until=now + timedelta(seconds=90),
        )
        self.binding = RoutingTeamBinding.objects.create(
            organization_id=1, routing_team=self.team, subject=self.subject,
            state='active', version=1, is_current=True, subject_generation=1,
            command_id=uuid.uuid4(), digest='3' * 64,
        )
        self.entitlement = ReceiveAssignmentEntitlement.objects.create(
            organization_id=1, subject=self.subject, routing_team=self.team,
            project=None, workflow_name=self.item.workflow_name,
            action='receive_assignment', state='active', version=1,
            is_current=True, subject_generation=1,
            grant_source='reviewed_task:PLAT-4572', grant_issuer_id=77,
            grant_issuer_email='ren@shizuha.com', grant_event_id=uuid.uuid4(),
            command_id=uuid.uuid4(), digest='4' * 64,
        )

    def test_binding_is_candidate_only_and_preserves_security_roster_bytes(self):
        snapshot = binding_candidates_for_item('cortex', self.item)
        self.assertEqual(snapshot['candidates'][0]['user_id'], 88)
        self.assertEqual(TeamRoster.objects.filter(team_name='cortex').count(), 0)
        self.assertEqual(list(TeamRoster.objects.filter(team_name='security').values(
            'id', 'organization_slug', 'team_name', 'tiers', 'junior_emails',
            'is_active', 'note', 'updated_by')), self.security_before)
        self.assertFalse(is_item_team_member(self.ichi, self.item))
        self.assertFalse(is_item_participant(self.ichi, self.item))

    def test_exact_snapshot_never_falls_back_to_legacy_after_tombstone(self):
        """PLAT-4829: activation is sticky and rollback HOLDs on the tombstone."""
        TeamRoster.objects.create(
            team_name='cortex', organization_slug='shizuha',
            tiers=[['legacy@shizuha.com']], is_active=True,
        )
        roster_before = list(TeamRoster.objects.order_by('id').values(
            'id', 'organization_id', 'organization_slug', 'team_name', 'tiers',
            'updated_at',
        ))

        active = routing_candidate_snapshot_for_item('cortex', self.item)
        self.assertEqual(active['source'], 'routing_binding')
        self.assertEqual(active['organization_id'], 1)
        self.assertEqual(
            [candidate['email'] for candidate in active['candidates']],
            ['ichi@shizuha.com'],
        )
        self.assertIn('team:1;bindings:1:active', active['source_version'])

        self.binding.is_current = False
        self.binding.save(update_fields=['is_current'])
        RoutingTeamBinding.objects.create(
            organization_id=1, routing_team=self.team, subject=self.subject,
            state=RoutingTeamBinding.STATE_TOMBSTONED, version=2,
            is_current=True, subject_generation=1, command_id=uuid.uuid4(),
            digest='5' * 64,
        )
        rolled_back = routing_candidate_snapshot_for_item('cortex', self.item)
        self.assertEqual(rolled_back['source'], 'routing_binding')
        self.assertEqual(rolled_back['candidates'], [])
        self.assertEqual(rolled_back['reason'], 'routing_binding_missing')
        self.assertIn('bindings:2:tombstoned', rolled_back['source_version'])
        self.assertEqual(
            list(TeamRoster.objects.order_by('id').values(
                'id', 'organization_id', 'organization_slug', 'team_name', 'tiers',
                'updated_at',
            )),
            roster_before,
        )

    @patch.object(WorkflowService, '_merge_sod_candidate_reasons', return_value=[])
    @patch.object(WorkflowService, '_member_can_execute_from_status', return_value=True)
    @patch.object(WorkflowService, '_seat_satisfies_required_capability', return_value=True)
    @patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True)
    def test_detector_availability_uses_receive_entitlement_decision(self, *_):
        """Candidate presence alone cannot make detector/resolver look healthy."""
        lane = routing_candidate_snapshot_for_item('cortex', self.item)
        self.assertEqual(
            _org_health_candidate_availability(WorkflowService, lane, self.item),
            {'ichi@shizuha.com': ['available']},
        )

        self.entitlement.is_current = False
        self.entitlement.save(update_fields=['is_current'])
        self.assertEqual(
            _org_health_candidate_availability(WorkflowService, lane, self.item),
            {'ichi@shizuha.com': ['entitlement_missing']},
        )

    @patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True)
    def test_active_team_write_guard_uses_exact_binding_not_legacy_roster(self, _available):
        self.assertEqual(
            WorkflowService._active_team_candidate_emails(
                'cortex',
                item=self.item,
            ),
            {'ichi@shizuha.com'},
        )
        self.assertTrue(
            WorkflowService._email_in_current_team_active_tier(
                'cortex',
                'ichi@shizuha.com',
                item=self.item,
            ),
        )

    @patch.object(WorkflowService, '_agent_workable_load', return_value=0)
    @patch.object(WorkflowService, '_agent_active_load', return_value=0)
    @patch.object(WorkflowService, '_member_can_execute_from_status', return_value=True)
    @patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True)
    def test_live_seat_event_drains_exact_binding_queue_through_commit_fence(
        self,
        _available,
        _can,
        _active,
        _workable,
    ):
        from tasks.models import ItemActivity

        self.item.created_by_id = 77
        self.item.created_by_email = 'mio@shizuha.com'
        self.item.save(update_fields=['created_by_id', 'created_by_email'])

        result = sweep_assign_group_queued.run(
            only_agent_email='ichi@shizuha.com',
        )

        self.item.refresh_from_db()
        self.assertEqual(result['assigned'], 1)
        self.assertEqual(self.item.assignee_id, 88)
        self.assertEqual(self.item.assignee_email, 'ichi@shizuha.com')
        self.assertEqual(self.item.assignment_group, 'cortex')
        self.assertTrue(
            ItemActivity.objects.filter(
                item=self.item,
                action='assigned',
                user_id=0,
                user_email='system@pulse',
            ).exists(),
        )
        self.assertFalse(
            ItemActivity.objects.filter(
                item=self.item,
                action='assigned',
                user_email='mio@shizuha.com',
            ).exists(),
        )

    @patch.object(WorkflowService, '_agent_workable_load', return_value=0)
    @patch.object(WorkflowService, '_agent_active_load', return_value=0)
    @patch.object(WorkflowService, '_member_can_execute_from_status', return_value=True)
    @patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True)
    def test_item_aware_probe_uses_same_fenced_resolver(self, _available, _can, _active, _workable):
        self.client.force_authenticate(self.ichi)
        response = self.client.get(
            '/api/teams/resolve-assignee/',
            {'group': 'cortex', 'item_key': self.item.item_key},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['source'], 'routing_binding')
        self.assertEqual(response.data['reason'], 'authorized')
        self.assertEqual(response.data['organization_id'], 1)
        self.assertEqual(response.data['user_id'], 88)
        self.assertTrue(response.data['available'])

    def test_direct_writer_denied_but_commit_fenced_resolver_write_succeeds(self):
        before_status = self.item.status
        before_memberships = self.project.memberships.count()
        self.item.assignee_id = 88
        self.item.assignee_email = 'ichi@shizuha.com'
        with self.assertRaisesMessage(ValidationError, 'direct_writer_denied'):
            self.item.save(update_fields=['assignee_id', 'assignee_email'])
        self.item.refresh_from_db()
        self.assertIsNone(self.item.assignee_id)
        self.assertEqual(self.item.status, before_status)
        self.assertEqual(self.project.memberships.count(), before_memberships)

        fields = []
        WorkflowService._apply_resolved_assignee(
            self.item, 88, 'ichi@shizuha.com', fields,
        )
        self.item.save(update_fields=['assignee_id', 'assignee_email'])
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_id, 88)
        self.assertTrue(is_item_participant(self.ichi, self.item))
        self.assertEqual(self.project.memberships.count(), before_memberships)
        receipt = RoutingAssignmentReceipt.objects.get(item=self.item)
        self.assertIsNotNone(receipt.consumed_at)
        self.assertEqual(len(receipt.freshness_vector_digest), 64)

    def test_reviewer_authorized_lane_can_clear_owner_to_group_queue(self):
        fields = []
        WorkflowService._apply_resolved_assignee(
            self.item, 88, 'ichi@shizuha.com', fields,
        )
        self.item.save(update_fields=['assignee_id', 'assignee_email'])

        self.assertTrue(self.item.unassign(reason='no_candidate'))

        self.item.refresh_from_db()
        self.assertIsNone(self.item.assignee_id)
        self.assertEqual(self.item.assignee_email or '', '')
        self.assertEqual(self.item.assignment_group, 'cortex')
        receipt = RoutingAssignmentReceipt.objects.get(
            item=self.item, action=RoutingAssignmentReceipt.ACTION_CLEAR,
        )
        self.assertEqual(receipt.reason, 'no_candidate')
        self.assertEqual(receipt.subject_id, 88)
        self.assertEqual(receipt.assignee_email, 'ichi@shizuha.com')
        self.assertIsNotNone(receipt.consumed_at)

    def _assign_ichi(self, item=None):
        item = item or self.item
        persisted_group = Item.objects.filter(pk=item.pk).values_list(
            'assignment_group', flat=True,
        ).first()
        fields = (
            ['assignment_group']
            if (persisted_group or '') != (item.assignment_group or '') else []
        )
        WorkflowService._apply_resolved_assignee(
            item, 88, 'ichi@shizuha.com', fields,
        )
        item.save(update_fields=list(dict.fromkeys([
            'assignee_id', 'assignee_email', *fields,
        ])))
        return item

    def _staff_actor(self, suffix):
        return User.objects.create_user(
            username=f'plat4572-{suffix}',
            email=f'plat4572-{suffix}@example.com',
            password='x', is_staff=True,
        )

    def test_public_unassign_clears_guarded_lane_with_exact_result_receipt(self):
        self._assign_ichi()
        self.client.force_authenticate(self._staff_actor('unassign'))

        response = self.client.post(
            f'/api/items/{self.item.id}/assign/', {}, format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.assignee_id)
        self.assertIsNone(self.item.assignment_group)
        receipt = RoutingAssignmentReceipt.objects.get(
            item=self.item,
            action=RoutingAssignmentReceipt.ACTION_UNASSIGN,
        )
        self.assertEqual(receipt.assignment_group, 'cortex')
        self.assertEqual(receipt.result_assignment_group, '')
        self.assertEqual(receipt.result_project_id, self.project.id)
        self.assertIsNotNone(receipt.consumed_at)

    @patch.object(WorkflowService, '_maybe_file_zero_member_team_alert', return_value=None)
    @patch.object(WorkflowService, '_resolve_team_assignee', return_value=(None, None))
    @patch.object(WorkflowService, '_is_known_team', return_value=True)
    def test_no_candidate_team_reroute_uses_exact_destination_receipt(self, *_):
        self._assign_ichi()
        self.client.force_authenticate(self._staff_actor('no-candidate'))

        response = self.client.post(
            f'/api/items/{self.item.id}/assign/',
            {'assignment_group': 'engineering'}, format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.assignee_id)
        self.assertEqual(self.item.assignment_group, 'engineering')
        receipt = RoutingAssignmentReceipt.objects.get(
            item=self.item,
            action=RoutingAssignmentReceipt.ACTION_REROUTE_GROUP_QUEUE,
        )
        self.assertEqual(receipt.assignment_group, 'cortex')
        self.assertEqual(receipt.result_assignment_group, 'engineering')
        self.assertIsNone(receipt.result_routing_team_id)
        self.assertIsNotNone(receipt.consumed_at)

    @patch.object(WorkflowService, '_maybe_file_zero_member_team_alert', return_value=None)
    @patch.object(WorkflowService, '_cap_applies', return_value=True)
    @patch.object(
        WorkflowService, '_resolve_team_assignee',
        side_effect=lambda *args, **kwargs: (
            (None, None)
            if kwargs.get('enforce_cap') else (88, 'ichi@shizuha.com')
        ),
    )
    @patch.object(WorkflowService, '_is_known_team', return_value=True)
    def test_capacity_queue_reroute_uses_exact_destination_receipt(self, *_):
        self._assign_ichi()
        self.client.force_authenticate(self._staff_actor('capacity'))

        response = self.client.post(
            f'/api/items/{self.item.id}/assign/',
            {'assignment_group': 'engineering'}, format='json',
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['queued_at_capacity'])
        receipt = RoutingAssignmentReceipt.objects.get(
            item=self.item,
            action=RoutingAssignmentReceipt.ACTION_REROUTE_GROUP_QUEUE,
        )
        self.assertEqual(receipt.result_assignment_group, 'engineering')
        self.assertIsNotNone(receipt.consumed_at)

    @patch.object(WorkflowService, '_resolve_team_assignee', return_value=(None, None))
    @patch.object(WorkflowService, '_is_team_member', return_value=False)
    def test_recurring_owner_group_drift_uses_exact_destination_receipt(self, *_):
        self._assign_ichi()
        Item.objects.filter(pk=self.item.pk).update(
            is_recurring=True,
            frequency='daily',
            due_date=timezone.now() + timedelta(days=1),
            metadata={'recurring_owner_team': 'engineering'},
        )
        self.item.refresh_from_db()

        self.assertTrue(WorkflowService._restore_recurring_owner_team(
            self.item, save=True,
        ))

        self.item.refresh_from_db()
        self.assertEqual(self.item.assignment_group, 'engineering')
        self.assertIsNone(self.item.assignee_id)
        receipt = RoutingAssignmentReceipt.objects.get(
            item=self.item,
            action=RoutingAssignmentReceipt.ACTION_REROUTE_GROUP_QUEUE,
        )
        self.assertEqual(receipt.reason, 'recurring_owner_unresolved')
        self.assertEqual(receipt.result_assignment_group, 'engineering')
        self.assertIsNotNone(receipt.consumed_at)

    def test_consumed_context_receipt_cannot_be_replayed(self):
        self._assign_ichi()
        reroute_assignment_to_group_queue(
            self.item, 'engineering', reason='no_candidate', save=True,
        )
        consumed = RoutingAssignmentReceipt.objects.get(
            item=self.item,
            action=RoutingAssignmentReceipt.ACTION_REROUTE_GROUP_QUEUE,
        )
        self.item.refresh_from_db()
        self.item.assignment_group = 'cortex'
        self._assign_ichi()
        baseline = {
            'tuple': (
                self.item.assignee_id, self.item.assignee_email,
                self.item.project_id, self.item.assignment_group,
                self.item.workflow_name, self.item.status,
            ),
            'receipts': RoutingAssignmentReceipt.objects.count(),
            'memberships': self.project.memberships.count(),
            'outbox': RoutingAuthorityOutbox.objects.count(),
        }

        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('pulse.routing_assignment_receipt', %s, true)",
                    [str(consumed.id)],
                )
            Item.objects.filter(pk=self.item.pk).update(
                assignee_id=None,
                assignee_email='',
                assignment_group='engineering',
            )

        self.item.refresh_from_db()
        self.assertEqual((
            self.item.assignee_id, self.item.assignee_email,
            self.item.project_id, self.item.assignment_group,
            self.item.workflow_name, self.item.status,
        ), baseline['tuple'])
        self.assertEqual(RoutingAssignmentReceipt.objects.count(), baseline['receipts'])
        self.assertEqual(self.project.memberships.count(), baseline['memberships'])
        self.assertEqual(RoutingAuthorityOutbox.objects.count(), baseline['outbox'])

    def test_concurrent_source_context_change_invalidates_reroute_decision(self):
        self._assign_ichi()
        reroute_assignment_to_group_queue(
            self.item, 'engineering', reason='no_candidate', save=False,
        )
        ReceiveAssignmentEntitlement.objects.create(
            organization_id=1,
            subject=self.subject,
            routing_team=self.team,
            project=None,
            workflow_name='next-flow',
            action='receive_assignment',
            state='active',
            version=1,
            is_current=True,
            subject_generation=1,
            grant_source='reviewed_task:PLAT-4572',
            grant_issuer_id=77,
            grant_issuer_email='ren@shizuha.com',
            grant_event_id=uuid.uuid4(),
            command_id=uuid.uuid4(),
            digest='f' * 64,
        )
        concurrent = Item.objects.get(pk=self.item.pk)
        concurrent.workflow_name = 'next-flow'
        fields = ['workflow_name']
        WorkflowService._apply_resolved_assignee(
            concurrent, 88, 'ichi@shizuha.com', fields,
        )
        concurrent.save(update_fields=list(dict.fromkeys([
            'workflow_name', 'assignee_id', 'assignee_email', *fields,
        ])))
        baseline = {
            'receipts': RoutingAssignmentReceipt.objects.count(),
            'memberships': self.project.memberships.count(),
            'outbox': RoutingAuthorityOutbox.objects.count(),
        }

        with self.assertRaisesMessage(ValidationError, 'direct_writer_denied'):
            self.item.save(update_fields=[
                'assignee_id', 'assignee_email', 'assignment_group',
            ])

        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_id, 88)
        self.assertEqual(self.item.assignment_group, 'cortex')
        self.assertEqual(self.item.workflow_name, 'next-flow')
        self.assertEqual(RoutingAssignmentReceipt.objects.count(), baseline['receipts'])
        self.assertEqual(self.project.memberships.count(), baseline['memberships'])
        self.assertEqual(RoutingAuthorityOutbox.objects.count(), baseline['outbox'])

    def test_review_permission_and_no_candidate_clears_leave_durable_holds(self):
        reasons = ('no_candidate', 'permission_drift', 'review_starvation')
        for index, reason in enumerate(reasons, start=60):
            item = Item.objects.create(
                mode='task', item_type='task.bug', title=f'{reason} clear',
                project=self.project, item_key=f'SCLX-{index}',
                workflow_name=self.item.workflow_name,
                assignment_group='cortex', status='open',
            )
            fields = []
            WorkflowService._apply_resolved_assignee(
                item, 88, 'ichi@shizuha.com', fields,
            )
            item.save(update_fields=['assignee_id', 'assignee_email'])

            hold_fields = []
            if reason == 'review_starvation':
                WorkflowService._set_merge_sod_hold(
                    item,
                    reason='no_distinct_available_merger',
                    reviewers={'kai2574'}, excluded={'ichi@shizuha.com'},
                    unknown=set(), fields=hold_fields,
                )
            else:
                WorkflowService._record_routability_hold(
                    item, 'cortex', [reason], source=f'test-{reason}',
                )
            item.unassign(save=False, reason=reason)
            item.save(update_fields=list(dict.fromkeys([
                'assignee_id', 'assignee_email', *hold_fields,
            ])))

            item.refresh_from_db()
            self.assertIsNone(item.assignee_id)
            self.assertEqual(item.assignment_group, 'cortex')
            receipt = RoutingAssignmentReceipt.objects.get(
                item=item, action=RoutingAssignmentReceipt.ACTION_CLEAR,
            )
            self.assertEqual(receipt.reason, reason)
            self.assertIsNotNone(receipt.consumed_at)
            if reason == 'review_starvation':
                self.assertEqual(
                    item.metadata['merge_sod_hold']['reason'],
                    'no_distinct_available_merger',
                )
            else:
                self.assertTrue(Comment.objects.filter(
                    item=item,
                    origin_event='resolver.routability_hold',
                    origin_payload__source=f'test-{reason}',
                ).exists())

    def test_direct_raw_and_context_escape_clears_have_zero_side_effects(self):
        fields = []
        WorkflowService._apply_resolved_assignee(
            self.item, 88, 'ichi@shizuha.com', fields,
        )
        self.item.save(update_fields=['assignee_id', 'assignee_email'])
        baseline = {
            'tuple': (
                self.item.assignee_id, self.item.assignee_email,
                self.item.project_id, self.item.assignment_group,
                self.item.workflow_name, self.item.status,
            ),
            'memberships': self.project.memberships.count(),
            'outbox': RoutingAuthorityOutbox.objects.count(),
            'receipts': RoutingAssignmentReceipt.objects.count(),
        }

        self.item.assignee_id = None
        self.item.assignee_email = ''
        with self.assertRaisesMessage(ValidationError, 'direct_writer_denied'):
            self.item.save(update_fields=['assignee_id', 'assignee_email'])
        self.item.refresh_from_db()

        with self.assertRaises(DatabaseError), transaction.atomic():
            Item.objects.filter(pk=self.item.pk).update(
                assignee_id=None, assignee_email='',
            )

        self.item.refresh_from_db()
        self.item.unassign(save=False, reason='permission_drift')
        self.item.assignment_group = ''
        with self.assertRaisesMessage(ValidationError, 'direct_writer_denied'):
            self.item.save(update_fields=[
                'assignee_id', 'assignee_email', 'assignment_group',
            ])

        self.item.refresh_from_db()
        self.assertEqual((
            self.item.assignee_id, self.item.assignee_email,
            self.item.project_id, self.item.assignment_group,
            self.item.workflow_name, self.item.status,
        ), baseline['tuple'])
        self.assertEqual(self.project.memberships.count(), baseline['memberships'])
        self.assertEqual(RoutingAuthorityOutbox.objects.count(), baseline['outbox'])
        self.assertEqual(RoutingAssignmentReceipt.objects.count(), baseline['receipts'])

    def test_plain_model_save_and_queryset_update_cannot_bypass_assignment_fence(self):
        before = (self.item.assignee_id, self.item.assignee_email, self.item.status)
        self.item.assignee_id = 88
        self.item.assignee_email = 'ichi@shizuha.com'
        with self.assertRaisesMessage(ValidationError, 'direct_writer_denied'):
            self.item.save()  # no update_fields: the original P1 bypass
        self.item.refresh_from_db()
        self.assertEqual(
            (self.item.assignee_id, self.item.assignee_email, self.item.status), before)

        with self.assertRaises(DatabaseError), transaction.atomic():
            Item.objects.filter(pk=self.item.pk).update(
                assignee_id=88, assignee_email='ichi@shizuha.com')
        self.item.refresh_from_db()
        self.assertEqual(
            (self.item.assignee_id, self.item.assignee_email, self.item.status), before)

    def test_assignment_plus_lane_clear_cannot_escape_receipt_fence(self):
        before = {
            'tuple': (
                self.item.assignee_id, self.item.assignee_email,
                self.item.project_id, self.item.assignment_group,
                self.item.workflow_name, self.item.status,
            ),
            'memberships': self.project.memberships.count(),
            'outbox': RoutingAuthorityOutbox.objects.count(),
            'receipts': RoutingAssignmentReceipt.objects.count(),
        }
        with self.assertRaises(DatabaseError), transaction.atomic():
            Item.objects.filter(pk=self.item.pk).update(
                assignment_group='', assignee_id=88,
                assignee_email='ichi@shizuha.com',
            )
        self.item.refresh_from_db()
        self.assertEqual((
            self.item.assignee_id, self.item.assignee_email,
            self.item.project_id, self.item.assignment_group,
            self.item.workflow_name, self.item.status,
        ), before['tuple'])
        self.assertEqual(self.project.memberships.count(), before['memberships'])
        self.assertEqual(RoutingAuthorityOutbox.objects.count(), before['outbox'])
        self.assertEqual(RoutingAssignmentReceipt.objects.count(), before['receipts'])

    def test_assigned_item_project_move_cannot_escape_receipt_fence(self):
        fields = []
        WorkflowService._apply_resolved_assignee(
            self.item, 88, 'ichi@shizuha.com', fields,
        )
        self.item.save(update_fields=['assignee_id', 'assignee_email'])
        foreign_project = Project.objects.create(
            name='Foreign private', key='FRP2', org_id=2,
            access_level='private', is_active=True,
        )
        before = {
            'tuple': (
                self.item.assignee_id, self.item.assignee_email,
                self.item.project_id, self.item.assignment_group,
                self.item.workflow_name, self.item.status,
            ),
            'memberships': self.project.memberships.count(),
            'foreign_memberships': foreign_project.memberships.count(),
            'outbox': RoutingAuthorityOutbox.objects.count(),
            'receipts': RoutingAssignmentReceipt.objects.count(),
        }

        with self.assertRaises(DatabaseError), transaction.atomic():
            Item.objects.filter(pk=self.item.pk).update(project_id=foreign_project.id)
        self.item.refresh_from_db()
        self.assertEqual((
            self.item.assignee_id, self.item.assignee_email,
            self.item.project_id, self.item.assignment_group,
            self.item.workflow_name, self.item.status,
        ), before['tuple'])

        self.item.project = foreign_project
        # Organization immutability is the outer fence; the routing-authority
        # receipt check remains defense in depth for same-organization moves.
        with self.assertRaisesMessage(
            ValidationError, 'cannot be moved to another organization',
        ):
            self.item.save(update_fields=['project'])
        self.item.refresh_from_db()
        self.assertEqual(self.item.project_id, before['tuple'][2])
        self.assertEqual(self.item.status, before['tuple'][5])
        self.assertEqual(self.project.memberships.count(), before['memberships'])
        self.assertEqual(
            foreign_project.memberships.count(), before['foreign_memberships'])
        self.assertEqual(RoutingAuthorityOutbox.objects.count(), before['outbox'])
        self.assertEqual(RoutingAssignmentReceipt.objects.count(), before['receipts'])

    def test_revocation_between_resolve_and_commit_invalidates_freshness_vector(self):
        fields = []
        WorkflowService._apply_resolved_assignee(
            self.item, 88, 'ichi@shizuha.com', fields)
        self.entitlement.is_current = False
        self.entitlement.save(update_fields=['is_current'])
        ReceiveAssignmentEntitlement.objects.create(
            organization_id=1, subject=self.subject, routing_team=self.team,
            project=None, workflow_name=self.item.workflow_name,
            action='receive_assignment', state='revoked', version=2,
            is_current=True, subject_generation=1,
            grant_source=self.entitlement.grant_source, grant_issuer_id=77,
            grant_issuer_email='ren@shizuha.com', grant_event_id=uuid.uuid4(),
            revocation_high_water=1, command_id=uuid.uuid4(), digest='e' * 64)
        with self.assertRaisesMessage(ValidationError, 'entitlement_missing'):
            self.item.save(update_fields=['assignee_id', 'assignee_email'])
        self.item.refresh_from_db()
        self.assertIsNone(self.item.assignee_id)
        self.assertEqual(RoutingAssignmentReceipt.objects.count(), 0)

    def test_nonwaivable_assignment_path_matrix_hits_the_same_db_and_model_fence(self):
        paths = (
            'direct', 'post_function', 'request_changes', 'unblock',
            'reconcile', 'rebalance', 'failover', 'recurring', 'import',
            'capacity_pull', 'owner_backstop', 'diagnostics',
        )
        baseline = {
            'memberships': self.project.memberships.count(),
            'outbox': RoutingAuthorityOutbox.objects.count(),
            'receipts': RoutingAssignmentReceipt.objects.count(),
        }
        for index, source in enumerate(paths, start=10):
            item = Item.objects.create(
                mode='task', item_type='task.bug', title=f'{source} parity',
                project=self.project, item_key=f'SCLX-{index}',
                assignment_group='cortex', status='open')
            persisted_status = item.status
            item.assignee_id = 88
            item.assignee_email = 'ichi@shizuha.com'
            with self.subTest(source=source), self.assertRaisesMessage(
                    ValidationError, 'direct_writer_denied'):
                item.save()
            item.refresh_from_db()
            self.assertIsNone(item.assignee_id)
            self.assertEqual(item.status, persisted_status)
        self.assertEqual(self.project.memberships.count(), baseline['memberships'])
        self.assertEqual(RoutingAuthorityOutbox.objects.count(), baseline['outbox'])
        self.assertEqual(RoutingAssignmentReceipt.objects.count(), baseline['receipts'])

    def test_revocation_denies_before_assignment_repair(self):
        fields = []
        WorkflowService._apply_resolved_assignee(
            self.item, 88, 'ichi@shizuha.com', fields,
        )
        self.item.save(update_fields=['assignee_id', 'assignee_email'])
        self.entitlement.is_current = False
        self.entitlement.save(update_fields=['is_current'])
        ReceiveAssignmentEntitlement.objects.create(
            organization_id=1, subject=self.subject, routing_team=self.team,
            project=None, workflow_name=self.item.workflow_name,
            action='receive_assignment', state='revoked', version=2,
            is_current=True, subject_generation=1,
            grant_source=self.entitlement.grant_source, grant_issuer_id=77,
            grant_issuer_email='ren@shizuha.com', grant_event_id=uuid.uuid4(),
            revocation_high_water=1, command_id=uuid.uuid4(), digest='5' * 64,
        )
        self.assertFalse(is_item_participant(self.ichi, self.item))
        self.assertEqual(
            subject_authorized_for_item(88, self.item, team_name='cortex').reason,
            'entitlement_missing',
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_id, 88, 'revocation must not shuffle the owner inline')

    def test_same_slug_foreign_org_binding_cannot_qualify(self):
        foreign_project = Project.objects.create(
            name='Foreign', key='FRGN', org_id=2, access_level='open', is_active=True,
        )
        foreign_item = Item.objects.create(
            mode='task', title='foreign cortex', project=foreign_project, item_key='FRGN-1',
            assignment_group='cortex', status='open',
        )
        snapshot = binding_candidates_for_item('cortex', foreign_item)
        self.assertIsNone(
            snapshot,
            'a same-named foreign aggregate cannot arm this organization',
        )

    def test_gate_off_is_sticky_hold_not_legacy_fallback(self):
        self.item.assignee_id = 88
        self.item.assignee_email = 'ichi@shizuha.com'
        WorkflowService._apply_resolved_assignee(
            self.item, 88, 'ichi@shizuha.com', [],
        )
        self.item.save(update_fields=['assignee_id', 'assignee_email'])
        with patch.dict('os.environ', {'PULSE_ROUTING_BINDINGS_ENFORCE': '0'}, clear=False):
            snapshot = binding_candidates_for_item('cortex', self.item)
            self.assertEqual(snapshot['reason'], 'routing_authority_gate_off')
            self.assertEqual(snapshot['candidates'], [])
            self.assertTrue(
                subject_authorized_for_item(88, self.item, team_name='cortex').allowed)
            self.assertTrue(is_item_participant(self.ichi, self.item))

    def test_private_cross_project_and_missing_open_entitlement_deny(self):
        private = Project.objects.create(
            name='Private', key='PRVX', org_id=1, access_level='private', is_active=True)
        private_item = Item.objects.create(
            mode='task', title='private cortex', project=private, item_key='PRVX-1',
            assignment_group='cortex', status='open')
        self.assertEqual(subject_authorized_for_item(
            88, private_item, team_name='cortex').reason, 'entitlement_missing')
        self.entitlement.is_current = False
        self.entitlement.save(update_fields=['is_current'])
        self.assertEqual(subject_authorized_for_item(
            88, self.item, team_name='cortex').reason, 'entitlement_missing')

    def test_scoped_cross_org_delegation_and_negative_scopes(self):
        observed = timezone.now()
        foreign = RoutingSubject.objects.create(
            organization_id=2, subject_id=99, canonical_email='foreign@shizuha.com',
            state='active', generation=1, digest='6' * 64,
            observed_at=observed, lease_until=observed + timedelta(seconds=90))
        RoutingAuthorityCheckpoint.objects.create(
            authority_id='shizuha-id', aggregate_key='2:99', subject_generation=1,
            observed_version=1, digest=foreign.digest, revocation_high_water=0,
            checkpoint_generation=1, source_event_id=uuid.uuid4(),
            first_received_at=observed,
            source_expires_at=observed + timedelta(seconds=90), observed_at=observed,
            lease_until=observed + timedelta(seconds=90))
        RoutingTeamBinding.objects.create(
            organization_id=1, routing_team=self.team, subject=foreign,
            state='active', version=1, is_current=True, subject_generation=1,
            command_id=uuid.uuid4(), digest='7' * 64)
        delegated = ReceiveAssignmentEntitlement.objects.create(
            organization_id=1, subject=foreign, routing_team=self.team,
            project=self.project, workflow_name=self.item.workflow_name,
            action='receive_assignment', state='active', version=1,
            is_current=True, subject_generation=1,
            grant_source='reviewed_cross_org:PLAT-4572', grant_issuer_id=77,
            grant_issuer_email='ren@shizuha.com', grant_event_id=uuid.uuid4(),
            expires_at=timezone.now() + timedelta(minutes=10),
            command_id=uuid.uuid4(), digest='8' * 64)
        with patch.object(WorkflowService, '_seat_satisfies_required_capability', return_value=True):
            self.assertEqual(subject_authorized_for_item(
                99, self.item, team_name='cortex').reason,
                'cross_org_delegation_missing')
        delegation = CrossOrgAssignmentDelegation.objects.create(
            source_organization_id=2, target_organization_id=1,
            subject=foreign, routing_team=self.team, project=self.project,
            action='receive_assignment', state='active', version=1,
            is_current=True, subject_generation=1, revocation_generation=0,
            expires_at=timezone.now() + timedelta(minutes=10),
            command_id=uuid.uuid4(), digest='9' * 64)
        with patch.object(WorkflowService, '_seat_satisfies_required_capability', return_value=True):
            self.assertTrue(subject_authorized_for_item(
                99, self.item, team_name='cortex').allowed)
            delegation.subject_generation = 2
            delegation.save(update_fields=['subject_generation'])
            self.assertEqual(subject_authorized_for_item(
                99, self.item, team_name='cortex').reason,
                'cross_org_delegation_subject_stale')
            delegation.subject_generation = 1
            delegation.save(update_fields=['subject_generation'])
            delegation.state = 'revoked'
            delegation.save(update_fields=['state'])
            self.assertEqual(subject_authorized_for_item(
                99, self.item, team_name='cortex').reason,
                'cross_org_delegation_missing')
            delegation.state = 'active'
            delegation.save(update_fields=['state'])
            delegation.expires_at = timezone.now() - timedelta(seconds=1)
            delegation.save(update_fields=['expires_at'])
            self.assertEqual(subject_authorized_for_item(
                99, self.item, team_name='cortex').reason,
                'cross_org_delegation_expired')

    def test_lease_bounds_and_future_clock_fail_closed(self):
        checkpoint = RoutingAuthorityCheckpoint.objects.get(
            authority_id='shizuha-id', aggregate_key='1:88')
        checkpoint.lease_until = checkpoint.observed_at + timedelta(seconds=91)
        checkpoint.save(update_fields=['lease_until'])
        self.assertEqual(subject_authorized_for_item(
            88, self.item, team_name='cortex').reason, 'authority_stale')

    def test_event_checkpoint_is_causal_and_worker_cannot_remote_renew(self):
        checkpoint = RoutingAuthorityCheckpoint.objects.get(
            authority_id='shizuha-id', aggregate_key='1:88')
        checkpoint.lease_until = timezone.now() - timedelta(seconds=1)
        checkpoint.save(update_fields=['lease_until'])
        old_generation = checkpoint.checkpoint_generation
        old_event = checkpoint.source_event_id
        with patch('tasks.services.routing_authority._resolve_identity') as remote:
            result = refresh_identity_checkpoints(limit=16)
        remote.assert_not_called()
        checkpoint.refresh_from_db()
        self.assertEqual(checkpoint.checkpoint_generation, old_generation)
        self.assertEqual(checkpoint.source_event_id, old_event)
        self.assertEqual(result['expired'], 1)
        self.assertEqual(RoutingAuthorityHoldSignal.objects.filter(
            authority_id='shizuha-id', aggregate_key='1:88',
            reason='authority_stale').count(), 1)

        event_id = uuid.uuid4()
        observed = timezone.now()
        renewed = apply_authority_checkpoint(
            authority_id='shizuha-id', aggregate_key='1:88',
            subject_generation=1, observed_version=2, digest=self.subject.digest,
            revocation_high_water=0, source_event_id=event_id,
            observed_at=observed,
            source_expires_at=observed + timedelta(seconds=45))
        self.assertEqual(renewed.checkpoint_generation, old_generation + 1)
        self.assertLessEqual(renewed.lease_until, renewed.source_expires_at)
        replay = apply_authority_checkpoint(
            authority_id='shizuha-id', aggregate_key='1:88',
            subject_generation=1, observed_version=2, digest=self.subject.digest,
            revocation_high_water=0, source_event_id=event_id,
            observed_at=observed,
            source_expires_at=observed + timedelta(seconds=45))
        self.assertEqual(replay.checkpoint_generation, renewed.checkpoint_generation)
        with self.assertRaisesMessage(RoutingAuthorityError, 'checkpoint_digest_conflict'):
            apply_authority_checkpoint(
                authority_id='shizuha-id', aggregate_key='1:88',
                subject_generation=1, observed_version=2, digest='f' * 64,
                revocation_high_water=0, source_event_id=uuid.uuid4(),
                observed_at=observed)

    def test_hold_signal_dedup_and_full_jitter_backoff_are_scope_bounded(self):
        checkpoint = RoutingAuthorityCheckpoint.objects.get(
            authority_id='shizuha-id', aggregate_key='1:88')
        checkpoint.lease_until = timezone.now() - timedelta(seconds=1)
        checkpoint.save(update_fields=['lease_until'])
        with patch(
                'tasks.services.routing_authority._notify_routing_authority_hold',
                return_value=(True, 'ichi@shizuha.com'),
        ) as notify, patch(
                'tasks.services.routing_authority.random.uniform', return_value=17,
        ):
            first = refresh_identity_checkpoints(limit=16)
            second = refresh_identity_checkpoints(limit=16)
        self.assertEqual(RoutingAuthorityHoldSignal.objects.filter(
            authority_id='shizuha-id', aggregate_key='1:88',
            reason='authority_stale').count(), 1)
        signal = RoutingAuthorityHoldSignal.objects.get(
            authority_id='shizuha-id', aggregate_key='1:88',
            reason='authority_stale')
        self.assertEqual(first['claimed'], 1)
        self.assertEqual(second['claimed'], 0)
        notify.assert_called_once()
        self.assertFalse(signal.in_flight)
        self.assertEqual(signal.attempts, 1)
        self.assertGreater(signal.next_attempt_at, timezone.now())
        self.assertIn('pulse_routing_authority_hold_scopes',
                      render_routing_authority_metrics())
        checkpoint.observed_at = timezone.now() + timedelta(seconds=16)
        checkpoint.lease_until = checkpoint.observed_at + timedelta(seconds=30)
        checkpoint.save(update_fields=['observed_at', 'lease_until'])
        self.assertEqual(subject_authorized_for_item(
            88, self.item, team_name='cortex').reason, 'authority_stale')

        now = timezone.now()
        for reason in ('coalesce_a', 'coalesce_b'):
            RoutingAuthorityHoldSignal.objects.create(
                authority_id='delegation', aggregate_key='one-exact-scope',
                checkpoint_generation=7, reason=reason, owner_group='devops',
                dedupe_key=uuid.uuid4().hex, next_attempt_at=now,
            )
        claimed = claim_freshness_reconciles(limit=16, now=now)
        same_scope = [row for row in claimed if row.aggregate_key == 'one-exact-scope']
        self.assertEqual(len(same_scope), 1)
        self.assertEqual(RoutingAuthorityHoldSignal.objects.filter(
            authority_id='delegation', aggregate_key='one-exact-scope',
            in_flight=True).count(), 1)

    def test_outbox_relay_is_distributed_bounded_fail_loud_and_jittered(self):
        row = RoutingAuthorityOutbox.objects.create(
            origin_event_id=uuid.uuid4(), authority_kind='routing_binding',
            aggregate_key='1:cortex:88', version=99, digest='c' * 64,
            payload={'event': 'fixture'},
        )
        with patch.dict(os.environ, {'PULSE_ROUTING_OUTBOX_URL': ''}), patch(
                'random.uniform', return_value=17,
        ):
            first = relay_routing_authority_outbox.run()
            second = relay_routing_authority_outbox.run()
        row.refresh_from_db()
        self.assertEqual(first['claimed'], 1)
        self.assertEqual(first['failed'], 1)
        self.assertEqual(second['claimed'], 0)
        self.assertFalse(row.in_flight)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.last_error, 'relay_unconfigured')
        self.assertGreater(row.next_attempt_at, timezone.now())
        self.assertTrue(RoutingAuthorityHoldSignal.objects.filter(
            authority_id='routing-outbox', aggregate_key=row.aggregate_key,
            checkpoint_generation=99, reason='relay_unconfigured',
            owner_group='devops').exists())
        metrics = render_routing_authority_metrics()
        self.assertIn('pulse_routing_authority_outbox_pending 1', metrics)

    def test_consumer_crash_before_commit_replays_without_false_ack(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.assertEqual(apply_consumer_event(
                    consumer='crash-fixture', authority_kind='routing_binding',
                    aggregate_key='scope', version=1, digest='d' * 64), 'applied')
                raise RuntimeError('crash after apply before ack')
        self.assertFalse(RoutingAuthorityConsumerState.objects.filter(
            consumer='crash-fixture', aggregate_key='scope').exists())
        self.assertEqual(apply_consumer_event(
            consumer='crash-fixture', authority_kind='routing_binding',
            aggregate_key='scope', version=1, digest='d' * 64), 'applied')

    def test_db_org_guard_rejects_bulk_bypass(self):
        foreign_team = RoutingTeam.objects.create(
            organization_id=2, key='cortex', display_name='Foreign Cortex',
            state='active', version=1, digest='9' * 64)
        row = RoutingTeamBinding(
            organization_id=1, routing_team=foreign_team, subject=self.subject,
            state='active', version=1, subject_generation=1,
            command_id=uuid.uuid4(), digest='a' * 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RoutingTeamBinding.objects.bulk_create([row])

    def test_consumer_duplicate_gap_conflict_and_tombstone_high_water(self):
        self.assertEqual(apply_consumer_event(
            consumer='admin', authority_kind='routing_binding', aggregate_key='k',
            version=1, digest='a' * 64), 'applied')
        self.assertEqual(apply_consumer_event(
            consumer='admin', authority_kind='routing_binding', aggregate_key='k',
            version=1, digest='a' * 64), 'duplicate')
        self.assertEqual(apply_consumer_event(
            consumer='admin', authority_kind='routing_binding', aggregate_key='k',
            version=1, digest='b' * 64), 'integrity_hold')
        self.assertEqual(apply_consumer_event(
            consumer='admin', authority_kind='routing_binding', aggregate_key='k',
            version=2, digest='c' * 64), 'applied')
        self.assertEqual(apply_consumer_event(
            consumer='admin', authority_kind='routing_binding', aggregate_key='k',
            version=1, digest='a' * 64), 'duplicate')
        self.assertEqual(apply_consumer_event(
            consumer='admin', authority_kind='routing_binding', aggregate_key='gap',
            version=3, digest='c' * 64), 'gap_hold')
        self.assertEqual(apply_consumer_event(
            consumer='admin', authority_kind='routing_binding', aggregate_key='t',
            version=1, digest='d' * 64, tombstone=True), 'applied')
        self.assertEqual(apply_consumer_event(
            consumer='admin', authority_kind='routing_binding', aggregate_key='t',
            version=1, digest='e' * 64), 'integrity_hold')
        self.assertEqual(apply_consumer_event(
            consumer='admin', authority_kind='', aggregate_key='poison',
            version=1, digest='not-a-digest'), 'poison_quarantined')
        quarantine = RoutingAuthorityQuarantine.objects.get(
            aggregate_key='poison', reason='schema_invalid')
        self.assertIsNone(quarantine.recovered_at)
        poison_cursor = RoutingAuthorityConsumerState.objects.get(
            consumer='admin', authority_kind='__invalid__', aggregate_key='poison')
        self.assertEqual(poison_cursor.applied_version, 0)
        self.assertEqual(poison_cursor.hold_reason, 'poison_quarantined')
        # Crash/retry of the same poison is idempotent and cannot clear HOLD.
        self.assertEqual(apply_consumer_event(
            consumer='admin', authority_kind='', aggregate_key='poison',
            version=1, digest='not-a-digest'), 'poison_quarantined')
        self.assertEqual(RoutingAuthorityQuarantine.objects.filter(
            aggregate_key='poison').count(), 1)

    def test_reviewed_command_is_payload_bound_idempotent_and_emits_one_outbox(self):
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={'tiers': [['maker@shizuha.com']], 'is_active': True})
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha',
            defaults={'tiers': [['checker@shizuha.com']], 'is_active': True})
        maker = SimpleNamespace(id=41, email='maker@shizuha.com')
        checker = SimpleNamespace(id=42, email='checker@shizuha.com')
        idem = uuid.uuid4()
        command = propose_command(
            actor=maker, command_type='create_team', organization_id=1,
            payload={'team_key': 'cortex-next', 'display_name': 'Cortex Next'},
            expected_version=0, reason='PLAT-4572 regression fixture',
            idempotency_key=idem,
        )
        replay = propose_command(
            actor=maker, command_type='create_team', organization_id=1,
            payload={'team_key': 'cortex-next', 'display_name': 'Cortex Next'},
            expected_version=0, reason='PLAT-4572 regression fixture',
            idempotency_key=idem,
        )
        self.assertEqual(command.id, replay.id)
        applied = approve_and_apply_command(
            actor=checker, command_id=command.id, payload_digest=command.payload_digest)
        self.assertEqual(applied.state, RoutingAuthorityCommand.STATE_APPLIED)
        self.assertEqual(RoutingAuthorityOutbox.objects.filter(
            aggregate_key=command.aggregate_key).count(), 1)
        with self.assertRaisesMessage(RoutingAuthorityError, 'approval_payload_conflict'):
            approve_and_apply_command(
                actor=checker, command_id=command.id, payload_digest='0' * 64)

    @patch('tasks.services.routing_authority._resolve_identity')
    def test_cross_org_delegation_is_a_distinct_reviewed_aggregate(self, resolve_identity):
        resolve_identity.return_value = (99, 'foreign@shizuha.com', True)
        foreign = RoutingSubject.objects.create(
            organization_id=2, subject_id=99, canonical_email='foreign@shizuha.com',
            state='active', generation=3, digest='b' * 64)
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={'tiers': [['maker@shizuha.com']], 'is_active': True})
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha',
            defaults={'tiers': [['checker@shizuha.com']], 'is_active': True})
        maker = SimpleNamespace(id=41, email='maker@shizuha.com')
        checker = SimpleNamespace(id=42, email='checker@shizuha.com')
        command = propose_command(
            actor=maker, command_type='grant_cross_org_delegation',
            organization_id=1,
            payload={
                'team_key': 'cortex', 'subject_email': 'foreign@shizuha.com',
                'subject_organization_id': 2, 'project_key': self.project.key,
                'workflow_name': self.item.workflow_name,
                'grant_source': 'reviewed:PLAT-4572',
                'grant_event_id': str(uuid.uuid4()),
                'expires_at': (timezone.now() + timedelta(minutes=10)).isoformat(),
            },
            expected_version=0, reason='explicit cross-org delegation',
            idempotency_key=uuid.uuid4())
        approve_and_apply_command(
            actor=checker, command_id=command.id,
            payload_digest=command.payload_digest)
        delegation = CrossOrgAssignmentDelegation.objects.get(subject=foreign)
        self.assertEqual(delegation.source_organization_id, 2)
        self.assertEqual(delegation.target_organization_id, 1)
        self.assertEqual(delegation.project_id, self.project.id)
        self.assertEqual(delegation.subject_generation, 3)
        self.assertEqual(RoutingAuthorityOutbox.objects.get(
            aggregate_key=command.aggregate_key).authority_kind,
            'cross_org_delegation')

    def test_idempotency_is_maker_reason_bound_and_role_revocation_fails_closed(self):
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={'tiers': [['maker@shizuha.com', 'other@shizuha.com']],
                      'is_active': True})
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha',
            defaults={'tiers': [['checker@shizuha.com']], 'is_active': True})
        maker = SimpleNamespace(id=41, email='maker@shizuha.com')
        other = SimpleNamespace(id=43, email='other@shizuha.com')
        checker = SimpleNamespace(id=42, email='checker@shizuha.com')
        idem = uuid.uuid4()
        command = propose_command(
            actor=maker, command_type='create_team', organization_id=1,
            payload={'team_key': 'cortex-role-race', 'display_name': 'Cortex Role Race'},
            expected_version=0, reason='exact reason', idempotency_key=idem)
        with self.assertRaisesMessage(RoutingAuthorityError, 'idempotency_payload_conflict'):
            propose_command(
                actor=other, command_type='create_team', organization_id=1,
                payload={'team_key': 'cortex-role-race', 'display_name': 'Cortex Role Race'},
                expected_version=0, reason='exact reason', idempotency_key=idem)
        engineering = TeamRoster.objects.get(
            team_name='engineering', organization_slug='shizuha')
        engineering.tiers = [['other@shizuha.com']]
        engineering.save(update_fields=['tiers', 'updated_at'])
        with self.assertRaisesMessage(RoutingAuthorityError, 'maker_rbac_denied'):
            approve_and_apply_command(
                actor=checker, command_id=command.id,
                payload_digest=command.payload_digest)

    def test_checker_withdrawal_before_commit_fails_with_zero_history_or_outbox(self):
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={'tiers': [['maker@shizuha.com']], 'is_active': True})
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha',
            defaults={'tiers': [['checker@shizuha.com']], 'is_active': True})
        maker = SimpleNamespace(id=41, email='maker@shizuha.com')
        checker = SimpleNamespace(id=42, email='checker@shizuha.com')
        command = propose_command(
            actor=maker, command_type='create_team', organization_id=1,
            payload={'team_key': 'checker-race', 'display_name': 'Checker Race'},
            expected_version=0, reason='checker race', idempotency_key=uuid.uuid4())
        review = TeamRoster.objects.get(
            team_name='review', organization_slug='shizuha')
        review.tiers = [[]]
        review.save(update_fields=['tiers', 'updated_at'])
        with self.assertRaisesMessage(RoutingAuthorityError, 'checker_rbac_denied'):
            approve_and_apply_command(
                actor=checker, command_id=command.id,
                payload_digest=command.payload_digest)
        command.refresh_from_db()
        self.assertEqual(command.state, RoutingAuthorityCommand.STATE_PROPOSED)
        self.assertFalse(RoutingAuthorityOutbox.objects.filter(
            aggregate_key=command.aggregate_key).exists())

    def test_crash_before_publish_rolls_back_command_effect_and_outbox(self):
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={'tiers': [['maker@shizuha.com']], 'is_active': True})
        TeamRoster.objects.update_or_create(
            team_name='review', organization_slug='shizuha',
            defaults={'tiers': [['checker@shizuha.com']], 'is_active': True})
        maker = SimpleNamespace(id=41, email='maker@shizuha.com')
        checker = SimpleNamespace(id=42, email='checker@shizuha.com')
        command = propose_command(
            actor=maker, command_type='create_team', organization_id=1,
            payload={'team_key': 'crash-before-publish', 'display_name': 'Crash Fixture'},
            expected_version=0, reason='atomic publish fixture',
            idempotency_key=uuid.uuid4())
        with patch(
                'tasks.services.routing_authority._write_outbox',
                side_effect=RuntimeError('publish crash'),
        ), self.assertRaisesMessage(RuntimeError, 'publish crash'):
            approve_and_apply_command(
                actor=checker, command_id=command.id,
                payload_digest=command.payload_digest)
        command.refresh_from_db()
        self.assertEqual(command.state, RoutingAuthorityCommand.STATE_PROPOSED)
        self.assertFalse(RoutingTeam.objects.filter(
            organization_id=1, key='crash-before-publish').exists())
        self.assertFalse(RoutingAuthorityOutbox.objects.filter(
            aggregate_key=command.aggregate_key).exists())

    def test_assignment_writer_source_scan_has_no_queryset_bulk_bypass(self):
        root = Path(__file__).resolve().parents[2] / 'tasks'
        offenders = []
        for path in root.rglob('*.py'):
            if 'migrations' in path.parts or 'tests' in path.parts:
                continue
            text = path.read_text(encoding='utf-8')
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {'update', 'bulk_update'}:
                    continue
                call = ast.get_source_segment(text, node) or ''
                receiver = ast.get_source_segment(text, node.func.value) or ''
                if ('assignee_id' in call or 'assignee_email' in call) and (
                        'objects' in receiver or receiver.endswith(('queryset', 'qs'))):
                    offenders.append(
                        f'{path.relative_to(root.parent)}:{getattr(node, "lineno", "?")}')
        self.assertEqual(offenders, [], 'assignment QuerySet/bulk bypass writers found')
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT count(*) FROM pg_trigger
                 WHERE tgname = 'tasks_item_assignment_guard'
                   AND NOT tgisinternal
            """)
            self.assertEqual(cursor.fetchone()[0], 1)


class PLS539CrossOrgCeoDecisionsTests(TestCase):
    """PLS-539: ceo-decisions never canonicalizes to human-ops (I1), and a direct
    named-decider assignment is validated by the person's ORG binding, not by
    membership in the task's CURRENT team (I2/I3)."""

    def setUp(self):
        # A CEO Decisions team in a NON-default org (mirrors shizuha-trading 118).
        TeamRoster.objects.update_or_create(
            team_name='ceo-decisions', organization_slug='shizuha-trading',
            defaults={'tiers': [['operator@example.test']], 'is_active': True})
        # A same-org named decider who is on 'engineering', not on the task team.
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={'tiers': [['kai@shizuha.com']], 'is_active': True})
        self.proj_trading = Project.objects.create(
            name='TRD', key='TRDX', org_id=3, access_level='open')
        self.proj_shizuha = Project.objects.create(
            name='PLS', key='PLSX', org_id=1, access_level='open')

    def _orgmap(self, org_id):
        return {1: {'id': 1, 'slug': 'shizuha'},
                3: {'id': 3, 'slug': 'shizuha-trading'}}.get(int(org_id))

    def test_ceo_decisions_never_canonicalizes_to_human_ops(self):
        # I1 across all three server/client alias tables.
        from tasks.models import TeamRoster as _TR
        self.assertEqual(WorkflowService._canonical_team_name('ceo-decisions'), 'ceo-decisions')
        self.assertNotIn('ceo-decisions', _TR._CANONICAL_ALIASES)
        # A TeamRoster row keeps its ceo-decisions identity (not rewritten).
        r = _TR(team_name='ceo-decisions', organization_slug='shizuha-trading')
        r.team_name = _TR._CANONICAL_ALIASES.get('ceo-decisions', 'ceo-decisions')
        self.assertEqual(r.team_name, 'ceo-decisions')

    def test_ceo_decisions_group_routes_within_task_org(self):
        # I1: routing ceo-decisions in shizuha-trading resolves to its member
        # (Hritik) — NOT rejected via a nonexistent human-ops roster.
        item = Item(project=self.proj_trading, status='blocked', assignment_group='ceo-decisions')
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            mismatch, roster_org, task_org = WorkflowService.assignment_org_mismatch(
                item, 'operator@example.test', 'ceo-decisions')
        self.assertFalse(mismatch, 'ceo-decisions lead must be assignable in the task org')
        self.assertEqual(task_org, 'shizuha-trading')

    def test_same_org_named_decider_direct_assign_succeeds(self):
        # I2/I3 (the same-org evidence): a valid same-org member assigned DIRECTLY
        # (team_name=None) must NOT be gated on the task's CURRENT team roster.
        item = Item(project=self.proj_shizuha, status='in_review', assignment_group='review')
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            # Direct assign (no requested team): validated by org binding.
            direct = WorkflowService.assignment_org_mismatch(
                item, 'kai@shizuha.com', None)[0]
            # Explicit team route to 'review' (kai not a member) is still denied.
            team_route = WorkflowService.assignment_org_mismatch(
                item, 'kai@shizuha.com', 'review')[0]
        self.assertFalse(direct, 'same-org named-decider direct assign must succeed (I2/I3)')
        self.assertTrue(team_route, 'explicit team route still validates team membership')

    def test_cross_org_direct_assign_still_denied(self):
        # The guard must still reject a genuinely cross-org agent (no I1/I2 regression).
        item = Item(project=self.proj_trading, status='blocked', assignment_group='')
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            mismatch = WorkflowService.assignment_org_mismatch(
                item, 'kai@shizuha.com', None)[0]
        self.assertTrue(mismatch, 'a shizuha agent must not be directly assignable to a shizuha-trading task')

    def test_assignment_denial_reason_is_honest(self):
        # I4: the same-org team-membership denial must NOT render the misleading
        # "serves org 'shizuha' but task belongs to org 'shizuha'".
        same_org = WorkflowService.assignment_denial_reason(
            'kai@shizuha.com', 'shizuha', 'shizuha')
        self.assertNotIn("serves org 'shizuha' but this task belongs to org 'shizuha'", same_org)
        self.assertIn('not authorized for this task', same_org)
        # A genuine cross-org denial still names both distinct orgs.
        cross = WorkflowService.assignment_denial_reason(
            'kai@shizuha.com', 'shizuha', 'shizuha-trading')
        self.assertIn("'shizuha'", cross)
        self.assertIn("'shizuha-trading'", cross)
        # Sentinels render their explanation, not a bogus org name.
        nit = WorkflowService.assignment_denial_reason('x@y.com', 'not-in-team', 'shizuha')
        self.assertIn('target team roster', nit)


class PLS539DirectOverrideEntitlementTests(TestCase):
    """PLS-539 P1 #2: a direct override authorizes an agent by EXACT task-org
    entitlement (reviewed binding when the lane is materialized, else exact-org
    roster) — never a first-match roster org that mis-rejects multi-org
    identities in the task org."""

    def setUp(self):
        self.proj_digital = Project.objects.create(
            name='BKSD2', key='BKSD2', org_id=2, access_level='open')
        self.proj_global = Project.objects.create(
            name='PLATY', key='PLATY', org_id=1, access_level='open')

    def _orgmap(self, org_id):
        return {1: {'id': 1, 'slug': 'shizuha'},
                2: {'id': 2, 'slug': 'shizuha-digital'},
                3: {'id': 3, 'slug': 'shizuha-trading'}}.get(org_id)

    def test_email_in_org_roster_is_exact_not_first_match(self):
        TeamRoster.objects.create(
            team_name='eng', tiers=[['multi@shizuha.com']],
            organization_slug='shizuha', is_active=True)
        TeamRoster.objects.create(
            team_name='digital-eng', tiers=[['multi@shizuha.com']],
            organization_slug='shizuha-digital', is_active=True)
        self.assertTrue(WorkflowService._email_in_org_roster('multi@shizuha.com', 'shizuha-digital'))
        self.assertTrue(WorkflowService._email_in_org_roster('multi@shizuha.com', 'shizuha'))
        self.assertFalse(WorkflowService._email_in_org_roster('multi@shizuha.com', 'shizuha-trading'))
        self.assertFalse(WorkflowService._email_in_org_roster('nobody@shizuha.com', 'shizuha'))

    def test_multiorg_direct_override_authorized_by_task_org_not_first_match(self):
        # Agent rostered in BOTH shizuha (potential first-match) and the task org.
        TeamRoster.objects.create(
            team_name='eng', tiers=[['multi@shizuha.com']],
            organization_slug='shizuha', is_active=True)
        TeamRoster.objects.create(
            team_name='digital-eng', tiers=[['multi@shizuha.com']],
            organization_slug='shizuha-digital', is_active=True)
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            mismatch, roster_org, task_org = WorkflowService.assignment_org_mismatch(
                Item(project=self.proj_digital), 'multi@shizuha.com', None, user_id=None)
        self.assertFalse(mismatch, f'multi-org agent wrongly rejected ({roster_org} vs {task_org})')
        self.assertEqual(task_org, 'shizuha-digital')

    def test_direct_override_rejected_when_not_in_task_org(self):
        TeamRoster.objects.create(
            team_name='eng', tiers=[['only-global@shizuha.com']],
            organization_slug='shizuha', is_active=True)
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap), \
             patch('tasks.services.admin_service.admin_service.get_org_members', return_value=[]), \
             patch('tasks.services.admin_service.admin_service.is_org_member', return_value=False):
            mismatch, _ro, task_org = WorkflowService.assignment_org_mismatch(
                Item(project=self.proj_digital), 'only-global@shizuha.com', None, user_id=None)
        self.assertTrue(mismatch)
        self.assertEqual(task_org, 'shizuha-digital')

    # test_materialized_lane_uses_signed_entitlement removed (hiro/ADR-0010): it
    # mocked routing_authority_applies_to_item + subject_authorized_for_item and
    # only exercised assignment_org_mismatch, never the guarded Item.save. Its
    # replacement drives the REAL guarded save + DB triggers through the API —
    # see tasks/tests/test_pls539_direct_handoff_crossing.py.


class PLS539DirectOverrideAPITests(APITestCase):
    """PLS-539 (revi #313): the REAL API PATCH and /assign direct-override paths —
    a direct assignee is validated by task-org binding (not the persisted team),
    and a direct override clears the stale assignment_group."""

    def setUp(self):
        self.client = APIClient()
        self.project = Project.objects.create(
            name='DBKS2', key='DBKS2', org_id=2, access_level='open', is_active=True)
        self.item = Item.objects.create(
            mode='task', title='t', project=self.project, assignment_group='team-x')
        self.actor = User.objects.create_user(
            username='actor539', email='actor539@shizuha.com', password='x',
            is_staff=True, is_superuser=True)
        # Direct assignee: an AGENT in the task org (shizuha-digital) roster, but
        # NOT in the item's persisted team 'team-x'. No User row -> treated as an
        # agent by _is_human_account, so the org-scoped roster check applies.
        TeamRoster.objects.create(
            team_name='digital-eng', organization_slug='shizuha-digital',
            tiers=[['dev539@shizuha.com']], is_active=True)

    @staticmethod
    def _orgmap(org_id):
        return {1: {'id': 1, 'slug': 'shizuha'},
                2: {'id': 2, 'slug': 'shizuha-digital'}}.get(org_id)

    def test_patch_direct_assignee_not_rejected_by_persisted_team(self):
        self.client.force_authenticate(self.actor)
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            resp = self.client.patch(
                f'/api/items/{self.item.id}/',
                {'assignee_email': 'dev539@shizuha.com',
                 'direct_assignment_reason': 'named decider handoff'},
                format='json')
        self.assertIn(resp.status_code, (200, 202), resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_email, 'dev539@shizuha.com')
        # revi #316 P1: a direct PATCH clears the stale team (direct-label honesty).
        self.assertIsNone(self.item.assignment_group,
                          "stale team 'team-x' not cleared on direct PATCH")
        # revi #47 P1: the direct override must persist the SAME durable audit as
        # create + /assign — the reason in a Comment AND the decision pin metadata,
        # so the reason is auditable and the handoff is exempt from rebalance churn.
        self.assertTrue(
            Comment.objects.filter(
                item=self.item, content__contains='named decider handoff').exists(),
            "direct-override audit comment (with reason) not written on PATCH")
        pin = (self.item.metadata or {}).get('direct_assignment')
        self.assertIsInstance(pin, dict, "direct_assignment pin metadata missing after PATCH")
        self.assertEqual(pin.get('assignee_email'), 'dev539@shizuha.com')
        self.assertIn('named decider handoff', pin.get('reason') or '')

    def test_patch_nondirect_reassignment_releases_obsolete_pin(self):
        # revi #47 P1: a NON-direct reassignment (a TEAM route — a group with NO
        # concrete assignee, so Pulse resolves the live member) must release a stale
        # direct-assignment pin, so the new team owner is not wrongly exempted from
        # rebalance/failover. (Under the uniform person-wins rule a group WITH a named
        # assignee is a direct override, not a team route, so the non-direct path is a
        # group-only payload.)
        TeamRoster.objects.create(
            team_name='digital-eng2', organization_slug='shizuha-digital',
            tiers=[['dev539b@shizuha.com']], is_active=True)
        # Seed a pinned direct handoff to dev539 (in digital-eng). Use .update()
        # so the pre-state is written without re-running the model save validator.
        Item.objects.filter(pk=self.item.pk).update(
            assignee_email='dev539@shizuha.com',
            assignment_group='digital-eng',
            metadata={'direct_assignment': {
                'assignee_email': 'dev539@shizuha.com', 'reason': 'old handoff'}})
        self.item.refresh_from_db()
        self.client.force_authenticate(self.actor)
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            resp = self.client.patch(
                f'/api/items/{self.item.id}/',
                {'assignment_group': 'digital-eng2'},  # team route: group, no assignee
                format='json')
        self.assertIn(resp.status_code, (200, 202), resp.content)
        self.item.refresh_from_db()
        # the owner is re-routed OFF the pinned direct owner (to the resolved member,
        # or group-queued when the member is unresolvable offline) — either way NOT the
        # stale direct owner, and the obsolete pin is released.
        self.assertNotEqual((self.item.assignee_email or '').lower(), 'dev539@shizuha.com',
                            'still pinned to the stale direct owner after team reassignment')
        self.assertNotIn('direct_assignment', self.item.metadata or {},
                         "obsolete direct-assignment pin not released on non-direct reassignment")

    def test_patch_direct_assignee_without_reason_rejected_zero_mutation(self):
        # revi #316 P1: an assignee-only PATCH with NO direct_assignment_reason
        # must be rejected (like create + /assign) with zero mutation — never take
        # the widened direct path silently.
        self.client.force_authenticate(self.actor)
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            resp = self.client.patch(
                f'/api/items/{self.item.id}/',
                {'assignee_email': 'dev539@shizuha.com'},  # no direct_assignment_reason
                format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.assignee_email)          # zero mutation
        self.assertEqual(self.item.assignment_group, 'team-x')  # stale group untouched

    def test_patch_assignee_with_unchanged_group_no_reason_typed_400_not_silent_200(self):
        # PLAT-7515: a PATCH carrying assignee_email + the item's UNCHANGED
        # assignment_group WITHOUT direct_assignment_reason was classified as a
        # team route (group present, no reason), silently dropped the assignee
        # hint, and returned 200 with the old owner intact — on an unchanged
        # group nothing re-resolves, so the request could only ever be a silent
        # no-op (the reported REST/MCP divergence). It must be a typed 400 with
        # zero mutation instead; the team-route drop stays load-bearing only
        # when the group actually CHANGES (test_plat1089_* tolerance).
        # Seed the old owner to a member of the item's OWN team (the assignment
        # org fence validates (org, team, assignee) membership even on raw
        # updates), then PATCH a different same-org member with the group
        # unchanged and no reason.
        TeamRoster.objects.create(
            team_name='team-x', organization_slug='shizuha-digital',
            tiers=[['old539@shizuha.com']], is_active=True)
        TeamRoster.objects.create(
            team_name='digital-eng2', organization_slug='shizuha-digital',
            tiers=[['dev539b@shizuha.com']], is_active=True)
        Item.objects.filter(pk=self.item.pk).update(assignee_email='old539@shizuha.com')
        self.item.refresh_from_db()
        self.client.force_authenticate(self.actor)
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            resp = self.client.patch(
                f'/api/items/{self.item.id}/',
                {'assignee_email': 'dev539b@shizuha.com',
                 'assignment_group': 'team-x'},  # unchanged group, no reason
                format='json')
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn('assignee_email', resp.json(), 'typed field error expected')
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_email, 'old539@shizuha.com')  # zero mutation
        self.assertEqual(self.item.assignment_group, 'team-x')

    def test_assign_direct_override_clears_stale_group(self):
        self.client.force_authenticate(self.actor)
        with patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap):
            resp = self.client.post(
                f'/api/items/{self.item.id}/assign/',
                {'assignee_email': 'dev539@shizuha.com',
                 'direct_assignment_reason': 'named decider handoff'},
                format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_email, 'dev539@shizuha.com')
        self.assertIsNone(self.item.assignment_group,
                          "stale team 'team-x' not cleared on direct override")


class PLS539CrossEntrypointDirectHandoffTests(APITestCase):
    """PLAT-4871/PLS-539 (revi #47 P1): create, /assign and PATCH share ONE
    transactional direct-handoff contract. This property gate proves the SAME
    direct and team mutations produce IDENTICAL outcomes across every entry point
    — success/rejection, assignee, cleared stale group, reason comment, decision
    pin — AND that the audit is atomic (an injected comment failure rolls the
    assignee back), closing the previously non-atomic /assign + PATCH windows."""

    TARGET = 'dev@shizuha.com'
    OUTSIDER = 'outsider@shizuha.com'  # in-org (shizuha-digital) but NOT in TEAM

    def setUp(self):
        self.client = APIClient()
        self.project = Project.objects.create(
            name='XEP2', key='XEP2', org_id=2, access_level='open', is_active=True)
        self.actor = User.objects.create_user(
            username='xentry', email='xentry@shizuha.com', password='x',
            is_staff=True, is_superuser=True)
        TeamRoster.objects.create(
            team_name='digital-eng', organization_slug='shizuha-digital',
            tiers=[[self.TARGET]], is_active=True)
        # an IN-ORG (shizuha-digital) subject who is NOT a member of digital-eng —
        # for the P1.2 negative fixture (retained-group exact-membership enforcement).
        TeamRoster.objects.create(
            team_name='digital-other', organization_slug='shizuha-digital',
            tiers=[[self.OUTSIDER]], is_active=True)
        self.client.force_authenticate(self.actor)

    @staticmethod
    def _orgmap(org_id):
        return {1: {'id': 1, 'slug': 'shizuha'},
                2: {'id': 2, 'slug': 'shizuha-digital'}}.get(org_id)

    def _fresh_item(self):
        return Item.objects.create(
            mode='task', title='t', project=self.project, assignment_group='team-x')

    # --- per-entry-point direct-handoff drivers -> (response, item_id) ---
    def _create_direct(self, reason='handoff reason'):
        body = {'mode': 'task', 'title': 't2', 'project': self.project.id,
                'assignee_email': self.TARGET}
        if reason is not None:
            body['direct_assignment_reason'] = reason
        resp = self.client.post('/api/items/', body, format='json')
        iid = resp.data.get('id') if resp.status_code < 300 else None
        return resp, iid

    def _assign_direct(self, reason='handoff reason'):
        item = self._fresh_item()
        body = {'assignee_email': self.TARGET}
        if reason is not None:
            body['direct_assignment_reason'] = reason
        resp = self.client.post(f'/api/items/{item.id}/assign/', body, format='json')
        return resp, item.id

    def _patch_direct(self, reason='handoff reason'):
        item = self._fresh_item()
        body = {'assignee_email': self.TARGET}
        if reason is not None:
            body['direct_assignment_reason'] = reason
        resp = self.client.patch(f'/api/items/{item.id}/', body, format='json')
        return resp, item.id

    def _drivers(self):
        return [('create', self._create_direct),
                ('assign', self._assign_direct),
                ('patch', self._patch_direct)]

    # --- revi #47 P1.2: id-only, team-route, and mixed group+reason drivers so the
    #     property gate exercises every representation across every entry point, not
    #     just email-only direct. TARGET_ID resolves to TARGET via the mocked
    #     identity source (id-only must classify identically to email-only). ---
    TARGET_ID = 7777
    TEAM = 'digital-eng'  # registered for the item's org (shizuha-digital / org 2)

    def _identity_patches(self):
        # per-id/email resolution (NOT a blanket return_value) so the ACTOR's own
        # created_by id<->email consistency still resolves correctly — a blanket
        # mock would resolve the actor's id to the target and trip the identity gate.
        target = {'id': self.TARGET_ID, 'email': self.TARGET, 'username': 'dev'}
        actor = {'id': self.actor.id, 'email': self.actor.email, 'username': 'xentry'}
        by_id = {self.TARGET_ID: target, self.actor.id: actor}
        by_email = {self.TARGET: target, self.actor.email.lower(): actor}
        return (patch('tasks.user_service.user_service.get_user_by_id',
                      side_effect=lambda uid, *a, **k: by_id.get(int(uid))),
                patch('tasks.user_service.user_service.get_user_by_email',
                      side_effect=lambda em, *a, **k: by_email.get((em or '').strip().lower())))

    def _create_id_only(self, reason='handoff reason'):
        body = {'mode': 'task', 'title': 'ti', 'project': self.project.id,
                'assignee_id': self.TARGET_ID}
        if reason is not None:
            body['direct_assignment_reason'] = reason
        resp = self.client.post('/api/items/', body, format='json')
        return resp, (resp.data.get('id') if resp.status_code < 300 else None)

    def _assign_id_only(self, reason='handoff reason'):
        item = self._fresh_item()
        body = {'assignee_id': self.TARGET_ID}
        if reason is not None:
            body['direct_assignment_reason'] = reason
        resp = self.client.post(f'/api/items/{item.id}/assign/', body, format='json')
        return resp, item.id

    def _patch_id_only(self, reason='handoff reason'):
        item = self._fresh_item()
        body = {'assignee_id': self.TARGET_ID}
        if reason is not None:
            body['direct_assignment_reason'] = reason
        resp = self.client.patch(f'/api/items/{item.id}/', body, format='json')
        return resp, item.id

    def _create_team(self, reason=None):
        body = {'mode': 'task', 'title': 'tt', 'project': self.project.id,
                'assignment_group': self.TEAM}
        if reason is not None:
            body['direct_assignment_reason'] = reason
        resp = self.client.post('/api/items/', body, format='json')
        return resp, (resp.data.get('id') if resp.status_code < 300 else None)

    def _assign_team(self, reason=None):
        item = self._fresh_item()
        body = {'assignment_group': self.TEAM}
        if reason is not None:
            body['direct_assignment_reason'] = reason
        resp = self.client.post(f'/api/items/{item.id}/assign/', body, format='json')
        return resp, item.id

    def _patch_team(self, reason=None):
        item = self._fresh_item()
        body = {'assignment_group': self.TEAM}
        if reason is not None:
            body['direct_assignment_reason'] = reason
        resp = self.client.patch(f'/api/items/{item.id}/', body, format='json')
        return resp, item.id

    def _create_mixed(self, reason='handoff reason', email=None):
        body = {'mode': 'task', 'title': 'tx', 'project': self.project.id,
                'assignee_email': email or self.TARGET, 'assignment_group': self.TEAM}
        if reason is not None:
            body['direct_assignment_reason'] = reason
        resp = self.client.post('/api/items/', body, format='json')
        return resp, (resp.data.get('id') if resp.status_code < 300 else None)

    def _assign_mixed(self, reason='handoff reason', email=None):
        item = self._fresh_item()
        body = {'assignee_email': email or self.TARGET, 'assignment_group': self.TEAM}
        if reason is not None:
            body['direct_assignment_reason'] = reason
        resp = self.client.post(f'/api/items/{item.id}/assign/', body, format='json')
        return resp, item.id

    def _patch_mixed(self, reason='handoff reason', email=None):
        item = self._fresh_item()
        body = {'assignee_email': email or self.TARGET, 'assignment_group': self.TEAM}
        if reason is not None:
            body['direct_assignment_reason'] = reason
        resp = self.client.patch(f'/api/items/{item.id}/', body, format='json')
        return resp, item.id

    def _assert_direct_outcome(self, name, iid):
        item = Item.objects.get(id=iid)
        self.assertEqual((item.assignee_email or '').lower(), self.TARGET, name)
        self.assertIsNone(item.assignment_group, f'{name}: stale group not cleared')
        self.assertTrue(
            Comment.objects.filter(item=item, content__contains='handoff reason').exists(),
            f'{name}: reason audit comment missing')
        self.assertIsInstance((item.metadata or {}).get('direct_assignment'), dict,
                              f'{name}: decision pin missing')

    def test_id_only_direct_identical_to_email_direct_across_entrypoints(self):
        # revi #47 P1 counterexample (a): an id-only direct handoff must be gated +
        # audited + pinned + group-cleared exactly like the email-only case; create
        # and /assign previously skipped the reason gate because the RAW email was
        # empty (the id was resolved only later, or never for the gate).
        p_id, p_email = self._identity_patches()
        for name, driver in [('create', self._create_id_only),
                             ('assign', self._assign_id_only),
                             ('patch', self._patch_id_only)]:
            with self.subTest(entry=name), p_id, p_email, patch(
                    'tasks.services.admin_service.admin_service.get_organization',
                    side_effect=self._orgmap):
                resp, iid = driver()
                self.assertIn(resp.status_code, (200, 201, 202), f'{name}: {resp.content}')
                self._assert_direct_outcome(name, iid)

    def test_id_only_reasonless_direct_rejected_zero_mutation_across_entrypoints(self):
        # the id-only gap: a reasonless id-only direct must be REJECTED with zero
        # mutation everywhere, just like the reasonless email-only direct.
        p_id, p_email = self._identity_patches()
        for name, driver in [('assign', self._assign_id_only), ('patch', self._patch_id_only)]:
            with self.subTest(entry=name), p_id, p_email, patch(
                    'tasks.services.admin_service.admin_service.get_organization',
                    side_effect=self._orgmap):
                resp, iid = driver(reason=None)
                self.assertIn(resp.status_code, (400, 403), f'{name}: {resp.content}')
                item = Item.objects.get(id=iid)
                self.assertIsNone(item.assignee_email, f'{name}: mutated on rejection')
                self.assertEqual(item.assignment_group, 'team-x',
                                 f'{name}: group touched on rejection')
        with self.subTest(entry='create'), p_id, p_email, patch(
                'tasks.services.admin_service.admin_service.get_organization',
                side_effect=self._orgmap):
            before = Item.objects.count()
            resp, _ = self._create_id_only(reason=None)
            self.assertIn(resp.status_code, (400, 403), resp.content)
            self.assertEqual(Item.objects.count(), before, 'create not rejected')

    def test_mixed_group_plus_reason_is_direct_across_entrypoints(self):
        # revi #47 P1 counterexample (b): group + assignee + reason is a DIRECT
        # override at EVERY entry point — direct assignee + reason audit + decision
        # pin. PATCH previously classified the non-empty group as team intent and
        # wrote no direct audit/pin — the exact divergence. person-wins: the
        # co-supplied group is RETAINED identically everywhere (the caller explicitly
        # asked for it; only a LEFTOVER stale group is cleared), matching the
        # authoritative create contract (test_assignment.CreateAssignmentFinalityTests).
        for name, driver in [('create', self._create_mixed),
                             ('assign', self._assign_mixed),
                             ('patch', self._patch_mixed)]:
            with self.subTest(entry=name), patch(
                    'tasks.services.admin_service.admin_service.get_organization',
                    side_effect=self._orgmap):
                resp, iid = driver()
                self.assertIn(resp.status_code, (200, 201, 202), f'{name}: {resp.content}')
                item = Item.objects.get(id=iid)
                self.assertEqual((item.assignee_email or '').lower(), self.TARGET, name)
                self.assertEqual(item.assignment_group, self.TEAM,
                                 f'{name}: co-supplied group must be retained on direct')
                self.assertTrue(
                    Comment.objects.filter(
                        item=item, content__contains='handoff reason').exists(),
                    f'{name}: reason audit comment missing')
                self.assertIsInstance((item.metadata or {}).get('direct_assignment'), dict,
                                      f'{name}: decision pin missing')

    def test_group_plus_reason_no_assignee_is_team_route_across_entrypoints(self):
        # revi #47 P1 round 4: a group + direct_assignment_reason with NO concrete
        # assignee is a TEAM route at EVERY entry point — a reason without an assignee
        # is meaningless and must NOT create a third fall-through state (which let
        # /assign clear the owner instead of routing the requested group). The group is
        # routed (kept), no direct audit/pin, owner not destroyed.
        for name, driver in [('create', self._create_team),
                             ('assign', self._assign_team),
                             ('patch', self._patch_team)]:
            with self.subTest(entry=name), patch(
                    'tasks.services.admin_service.admin_service.get_organization',
                    side_effect=self._orgmap):
                resp, iid = driver(reason='stale reason, no assignee')
                self.assertIn(resp.status_code, (200, 201, 202), f'{name}: {resp.content}')
                item = Item.objects.get(id=iid)
                self.assertEqual(item.assignment_group, self.TEAM,
                                 f'{name}: group not routed (owner cleared instead?)')
                self.assertFalse(
                    Comment.objects.filter(
                        item=item, content__contains='Direct assignee override').exists(),
                    f'{name}: wrote a direct audit comment for a team route')
                self.assertIsNone((item.metadata or {}).get('direct_assignment'),
                                  f'{name}: wrote a direct decision pin for a team route')

    def test_team_route_no_direct_audit_or_pin_group_kept_across_entrypoints(self):
        # a group with NO reason is a team route at every entry point: no direct
        # reason-audit comment, no decision pin, and the group is RETAINED (never
        # treated as a direct override). A co-supplied assignee would be a stale
        # hint; here none is supplied.
        for name, driver in [('create', self._create_team),
                             ('assign', self._assign_team),
                             ('patch', self._patch_team)]:
            with self.subTest(entry=name), patch(
                    'tasks.services.admin_service.admin_service.get_organization',
                    side_effect=self._orgmap):
                resp, iid = driver()
                self.assertIn(resp.status_code, (200, 201, 202), f'{name}: {resp.content}')
                item = Item.objects.get(id=iid)
                self.assertEqual(item.assignment_group, self.TEAM,
                                 f'{name}: team route dropped/changed the group')
                self.assertFalse(
                    Comment.objects.filter(
                        item=item, content__contains='Direct assignee override').exists(),
                    f'{name}: team route wrote a direct audit comment')
                self.assertIsNone((item.metadata or {}).get('direct_assignment'),
                                  f'{name}: team route wrote a direct decision pin')

    def test_mixed_group_plus_assignee_no_reason_is_team_route_across_entrypoints(self):
        # revi #47 P1.1 (the omitted matrix row): group + assignee + NO reason is a
        # TEAM route under the ONE uniform precedence — identical at EVERY entry point.
        # The stale assignee hint is DROPPED (never persisted as the owner) and NO
        # direct reason-audit comment or decision pin is written; the group is kept.
        for name, driver in [('create', self._create_mixed),
                             ('assign', self._assign_mixed),
                             ('patch', self._patch_mixed)]:
            with self.subTest(entry=name), patch(
                    'tasks.services.admin_service.admin_service.get_organization',
                    side_effect=self._orgmap):
                resp, iid = driver(reason=None)
                self.assertIn(resp.status_code, (200, 201, 202), f'{name}: {resp.content}')
                item = Item.objects.get(id=iid)
                self.assertNotEqual((item.assignee_email or '').lower(), self.TARGET,
                                    f'{name}: stale assignee hint persisted on a team route')
                self.assertEqual(item.assignment_group, self.TEAM,
                                 f'{name}: team route dropped/changed the group')
                self.assertFalse(
                    Comment.objects.filter(
                        item=item, content__contains='Direct assignee override').exists(),
                    f'{name}: team route wrote a direct audit comment')
                self.assertIsNone((item.metadata or {}).get('direct_assignment'),
                                  f'{name}: team route wrote a direct decision pin')

    def test_retained_group_exact_membership_enforced_across_entrypoints(self):
        # revi #47 P1.2 (negative fixture): a direct override that RETAINS a co-supplied
        # group must validate the assignee's EXACT (org, team) membership at every entry
        # point — an in-org subject OUTSIDE that team is rejected everywhere (previously
        # create rejected but /assign + PATCH accepted with org-only validation).
        for name, driver in [('create', self._create_mixed),
                             ('assign', self._assign_mixed),
                             ('patch', self._patch_mixed)]:
            with self.subTest(entry=name), patch(
                    'tasks.services.admin_service.admin_service.get_organization',
                    side_effect=self._orgmap):
                if name == 'create':
                    before = Item.objects.count()
                    resp, _ = driver(email=self.OUTSIDER)
                    self.assertIn(resp.status_code, (400, 403), resp.content)
                    self.assertEqual(Item.objects.count(), before,
                                     'create accepted an in-org non-member of the team')
                else:
                    resp, iid = driver(email=self.OUTSIDER)
                    self.assertIn(resp.status_code, (400, 403),
                                  f'{name}: in-org non-member of retained team accepted')
                    item = Item.objects.get(id=iid)
                    self.assertNotEqual((item.assignee_email or '').lower(), self.OUTSIDER,
                                        f'{name}: persisted a non-member as owner')

    def test_direct_handoff_success_identical_across_entrypoints(self):
        # PLAT-9427 triage (run 7478): Django's parallel runner pickles failing
        # subTest results back to the main process; that pickle crashes on the
        # WSGI-handler closure (convert_exception_to_response.<locals>.inner)
        # reachable from the test case, so the REAL assertion failure inside a
        # subTest is masked by `Can't get local object ...` and the suite
        # reports a corrupted errors=1 instead of the assertion. Collect
        # per-entry failures explicitly (picklable, parallel-safe) and fail
        # once with every detail.
        failures = []
        for name, driver in self._drivers():
            try:
                with patch(
                        'tasks.services.admin_service.admin_service.get_organization',
                        side_effect=self._orgmap):
                    resp, iid = driver()
                    self.assertIn(resp.status_code, (200, 201, 202),
                                  f'{name}: {resp.content}')
                    item = Item.objects.get(id=iid)
                    self.assertEqual(item.assignee_email, self.TARGET, name)
                    self.assertIsNone(item.assignment_group,
                                      f'{name}: stale group not cleared')
                    self.assertTrue(
                        Comment.objects.filter(
                            item=item, content__contains='handoff reason').exists(),
                        f'{name}: reason audit comment missing')
                    pin = (item.metadata or {}).get('direct_assignment')
                    self.assertIsInstance(pin, dict, f'{name}: decision pin missing')
                    self.assertEqual(pin.get('assignee_email'), self.TARGET, name)
            except self.failureException as exc:
                failures.append(f'{name}: {exc}')
        self.assertEqual(
            failures, [],
            f'direct-handoff entrypoint mismatches: {"; ".join(failures)}')

    def test_reasonless_direct_rejected_zero_mutation_across_entrypoints(self):
        for name, driver in [('assign', self._assign_direct), ('patch', self._patch_direct)]:
            with self.subTest(entry=name), patch(
                    'tasks.services.admin_service.admin_service.get_organization',
                    side_effect=self._orgmap):
                resp, iid = driver(reason=None)
                self.assertIn(resp.status_code, (400, 403), f'{name}: {resp.content}')
                item = Item.objects.get(id=iid)
                self.assertIsNone(item.assignee_email, f'{name}: mutated on rejection')
                self.assertEqual(item.assignment_group, 'team-x',
                                 f'{name}: stale group touched on rejection')
        with self.subTest(entry='create'), patch(
                'tasks.services.admin_service.admin_service.get_organization',
                side_effect=self._orgmap):
            before = Item.objects.count()
            resp, _ = self._create_direct(reason=None)
            self.assertIn(resp.status_code, (400, 403), resp.content)
            self.assertEqual(Item.objects.count(), before, 'create not rejected')

    def test_injected_comment_failure_rolls_back_assignee_atomically(self):
        # The atomicity guarantee: the audit comment is written in the SAME
        # transaction as the assignee, so an injected comment failure rolls the
        # assignee back (was a non-atomic window on /assign + PATCH).
        for name, driver in [('assign', self._assign_direct), ('patch', self._patch_direct)]:
            with self.subTest(entry=name), \
                 patch('tasks.services.admin_service.admin_service.get_organization',
                       side_effect=self._orgmap), \
                 patch('tasks.views.Comment.objects.create',
                       side_effect=RuntimeError('injected audit failure')):
                _resp, iid = driver()
                item = Item.objects.get(id=iid)
                self.assertIsNone(
                    item.assignee_email,
                    f'{name}: assignee persisted despite audit failure (NON-ATOMIC)')
                self.assertEqual(item.assignment_group, 'team-x',
                                 f'{name}: stale group cleared despite rolled-back handoff')
        with self.subTest(entry='create'), \
             patch('tasks.services.admin_service.admin_service.get_organization',
                   side_effect=self._orgmap), \
             patch('tasks.views.Comment.objects.create',
                   side_effect=RuntimeError('injected audit failure')):
            before = Item.objects.count()
            try:
                self._create_direct()  # DRF may surface as 500 rather than re-raise
            except Exception:
                pass
            # perform_create is @transaction.atomic — a failed audit rolls the whole
            # create back, so no partial item is persisted.
            self.assertEqual(Item.objects.count(), before,
                             'create not rolled back on audit failure')
