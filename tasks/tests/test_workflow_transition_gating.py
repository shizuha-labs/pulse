"""
Regression tests for workflow transition role-gating and post-function execution.
"""

import json
import os
import threading
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from tasks.models import (
    Comment,
    EscalationRoutingDisposition,
    EscalationRoutingNotificationOutbox,
    EscalationRoutingReceipt,
    EscalationRoutingRequest,
    Item,
    ItemLink,
    Notification,
    PublicationAttempt,
    PublicationBinding,
    PublicationConsumedRound,
    PublicationOutbox,
    Project,
    ProjectMembership,
    ReceiveAssignmentEntitlement,
    RoutingAssignmentReceipt,
    RoutingAuthorityCheckpoint,
    RoutingSubject,
    RoutingTeam,
    RoutingTeamBinding,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services import escalation_routing
from tasks.services.publication_handoff import _digest
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
from tasks.tasks import _run_workflow_stale_timers
from tasks.tasks import deliver_publication_outbox
from .base import create_blocked_item, satisfy_blocked_invariant


class WorkflowTransitionGatingTests(TestCase):
    """Ensure role-based transition gating is not bypassed by org membership."""

    def setUp(self):
        self.user_model = get_user_model()
        self.user = self.user_model.objects.create_user(
            username="agent_a",
            email="agent_a@example.com",
            password="testpass123",
        )
        self.user.organization_memberships = {1: "member"}
        self.project = Project.objects.create(
            name="Role Gate Project",
            key="RGP",
            org_id=1,
        )

    def _item(self):
        return SimpleNamespace(
            project=self.project,
            # _check_transition_conditions reads item.project_id (the FK id) for
            # the ProjectMembership gate; the real Item has both project and
            # project_id, so the fake must expose project_id too.
            project_id=self.project.id,
            assignee_id=None,
            assignee_email='',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )

    def _transition(self, allowed_roles):
        return SimpleNamespace(
            allowed_roles=allowed_roles,
            assignee_only=False,
            reporter_only=False,
        )

    def test_role_gating_denies_org_members_without_membership(self):
        """Users without a matching ProjectMembership must not pass role checks."""
        transition = self._transition(["admin"])
        item = self._item()

        self.assertFalse(
            WorkflowService._check_transition_conditions(
                transition,
                item,
                self.user,
            )
        )

    def test_role_gating_allows_matching_project_role(self):
        """Users with an allowed project role can execute the transition."""
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user.id,
            user_email=self.user.email,
            role="admin",
            is_active=True,
        )
        transition = self._transition(["admin", "member"])
        item = self._item()
        self.assertTrue(
            WorkflowService._check_transition_conditions(
                transition,
                item,
                self.user,
            )
        )

    def test_role_gating_allows_org_role_when_project_membership_missing(self):
        """Users with a matching org role can execute project-scoped transitions."""
        transition = self._transition(["admin"])
        item = self._item()
        self.user.organization_memberships = {1: "admin"}

        self.assertTrue(
            WorkflowService._check_transition_conditions(
                transition,
                item,
                self.user,
            )
        )

    def test_admin_ops_backup_can_request_workflow_change_revision_only(self):
        backup = self.user_model.objects.create_user(
            username='plat6037-backup',
            email='plat6037-backup@shizuha.com',
            password='testpass123',
        )
        roster, _ = TeamRoster.objects.get_or_create(
            team_name='admin-ops', organization_slug='shizuha', organization_id=1,
            defaults={'tiers': []},
        )
        roster.tiers = [[backup.email]]
        roster.updated_by = 'test:plat6037'
        roster.save(update_fields=['tiers', 'updated_by', 'updated_at'])
        proposed = Status.objects.create(
            slug='plat6037-proposed', name='Proposed PLAT6037', category='todo',
        )
        workflow = Workflow.objects.create(
            name='Workflow Change PLAT6037', slug='workflow-change',
            initial_status=proposed,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=proposed, position=0,
        )
        revision = WorkflowTransition.objects.create(
            workflow=workflow, name='Request Revision',
            from_status='under_review', to_status='proposed',
            allowed_roles=['review'],
        )
        item = Item.objects.create(
            mode='task', title='typed CR', project=self.project,
            workflow_name=workflow.name, status='under_review',
            item_key=self.project.get_next_item_key(),
        )

        self.assertTrue(WorkflowService._check_transition_conditions(revision, item, backup))

        approval = WorkflowTransition.objects.create(
            workflow=workflow, name='Approve',
            from_status='under_review', to_status='approved',
            allowed_roles=['review'],
        )
        self.assertFalse(WorkflowService._check_transition_conditions(approval, item, backup))

        engineer = self.user_model.objects.create_user(
            username='plat6037-engineer', email='plat6037-engineer@shizuha.com',
            password='testpass123',
        )
        self.assertFalse(WorkflowService._check_transition_conditions(revision, item, engineer))

    def test_role_gating_maps_security_role_aliases_to_admin(self):
        """Security-role aliases resolve through admin-equivalent mappings."""
        transition = self._transition(["security", "security-lead"])
        item = self._item()

        self.user.organization_memberships = {1: "member"}
        self.assertFalse(
            WorkflowService._check_transition_conditions(
                transition,
                item,
                self.user,
            )
        )

        self.user.organization_memberships = {1: "admin"}
        self.assertTrue(
            WorkflowService._check_transition_conditions(
                transition,
                item,
                self.user,
            )
        )

    def test_return_for_changes_routes_to_linked_origin_branch_owner(self):
        """PLS-568: bounce-back is an exact-org PLS-539 direct override."""
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            organization_id=1,
            team_name='engineering',
            defaults={'tiers': [['sara@shizuha.com']]},
        )
        item = Item.objects.create(
            project=self.project,
            title='Sara-owned branch returned from merge',
            mode='task',
            status='in_progress',
            workflow_name='autonomous-bug',
            assignment_group='review',
            assignee_id=None,
            assignee_email='',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
            metadata={
                'pull_requests': [{
                    'url': (
                        'https://origin.shizuha.com/shizuha-labs/'
                        'shizuha-tasks/pulls/47'
                    ),
                    'status': 'open',
                    'head_sha': 'abc123',
                }],
            },
        )
        transition = SimpleNamespace(
            id='return-branch-owner',
            name='Return for Changes',
            post_functions=[{'type': 'assign_team', 'value': 'engineering'}],
        )
        canonical_sara = {
            'id': 4,
            'username': 'sara',
            'email': 'sara@shizuha.com',
        }
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {'user': {'login': 'sara2574'}},
        )
        with patch('requests.get', return_value=response), patch(
            'tasks.user_service.user_service.get_user_by_username',
            return_value=canonical_sara,
        ), patch(
            'tasks.user_service.user_service.get_user_by_id',
            return_value=canonical_sara,
        ), patch(
            'tasks.services.admin_service.admin_service.get_organization',
            return_value={'id': 1, 'slug': 'shizuha'},
        ), patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), patch.object(
            WorkflowService,
            '_resolve_team_assignee',
        ) as least_load, patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'test-token',
            'ORIGIN_URL': 'https://origin.shizuha.com',
        }, clear=False):
            WorkflowService.execute_post_functions(transition, item)

        item.refresh_from_db()
        least_load.assert_not_called()
        self.assertEqual(item.assignee_id, 4)
        self.assertEqual(item.assignee_email, 'sara@shizuha.com')
        self.assertIsNone(item.assignment_group)
        marker = item.metadata['branch_return_routing']
        self.assertEqual(marker['state'], 'branch_owner')
        self.assertEqual(marker['author_logins'], ['sara2574'])
        self.assertEqual(marker['authorization']['task_org'], 'shizuha')

    def test_return_owner_crosses_materialized_lane_with_direct_receipt(self):
        """PLS-568: ADR-0041 authorization produces a consumed handoff receipt."""
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            organization_id=1,
            team_name='engineering',
            defaults={'tiers': [['sara@shizuha.com']]},
        )
        item = Item.objects.create(
            project=self.project,
            title='Sara branch returned from guarded Cortex lane',
            mode='task',
            status='in_progress',
            workflow_name='autonomous-bug',
            assignment_group='cortex',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
            metadata={
                'pull_requests': [{
                    'url': (
                        'https://origin.shizuha.com/shizuha-labs/'
                        'shizuha-tasks/pulls/568'
                    ),
                    'status': 'open',
                    'head_sha': uuid.uuid4().hex,
                }],
            },
        )
        team = RoutingTeam.objects.create(
            organization_id=1,
            key='cortex',
            display_name='Cortex',
            state='active',
            version=1,
            digest='1' * 64,
        )
        now = timezone.now()
        sara = RoutingSubject.objects.create(
            organization_id=1,
            subject_id=4,
            canonical_email='sara@shizuha.com',
            state='active',
            generation=1,
            digest='2' * 64,
            observed_at=now,
            lease_until=now + timedelta(seconds=90),
        )
        RoutingAuthorityCheckpoint.objects.create(
            authority_id='shizuha-id',
            aggregate_key='1:4',
            subject_generation=1,
            observed_version=1,
            digest=sara.digest,
            revocation_high_water=0,
            checkpoint_generation=1,
            source_event_id=uuid.uuid4(),
            first_received_at=now,
            source_expires_at=now + timedelta(seconds=90),
            observed_at=now,
            lease_until=now + timedelta(seconds=90),
        )
        RoutingTeamBinding.objects.create(
            organization_id=1,
            routing_team=team,
            subject=sara,
            state='active',
            version=1,
            is_current=True,
            subject_generation=1,
            command_id=uuid.uuid4(),
            digest='3' * 64,
        )
        ReceiveAssignmentEntitlement.objects.create(
            organization_id=1,
            subject=sara,
            routing_team=team,
            project=None,
            workflow_name=item.workflow_name,
            action='receive_assignment',
            state='active',
            version=1,
            is_current=True,
            subject_generation=1,
            grant_source='reviewed_task:PLS-568',
            grant_issuer_id=77,
            grant_issuer_email='ren@shizuha.com',
            grant_event_id=uuid.uuid4(),
            command_id=uuid.uuid4(),
            digest='4' * 64,
        )
        transition = SimpleNamespace(
            id='return-materialized-branch-owner',
            name='Return for Changes',
            post_functions=[{'type': 'assign_team', 'value': 'engineering'}],
        )
        canonical_sara = {
            'id': 4,
            'username': 'sara',
            'email': 'sara@shizuha.com',
        }
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {'user': {'login': 'sara2574'}},
        )

        with patch.dict(os.environ, {
            'PULSE_ROUTING_BINDINGS_ENFORCE': '1',
            'PULSE_ROUTING_BINDINGS_TEAMS': 'cortex',
            'FORGEJO_TOKEN': 'test-token',
            'ORIGIN_URL': 'https://origin.shizuha.com',
        }, clear=False), patch('requests.get', return_value=response), patch(
            'tasks.user_service.user_service.get_user_by_username',
            return_value=canonical_sara,
        ), patch(
            'tasks.user_service.user_service.get_user_by_id',
            return_value=canonical_sara,
        ), patch(
            'tasks.services.admin_service.admin_service.get_organization',
            return_value={'id': 1, 'slug': 'shizuha'},
        ), patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ):
            WorkflowService.execute_post_functions(transition, item)

        item.refresh_from_db()
        self.assertEqual(item.assignee_id, 4)
        self.assertEqual(item.assignee_email, 'sara@shizuha.com')
        self.assertIsNone(item.assignment_group)
        receipt = RoutingAssignmentReceipt.objects.get(
            item=item,
            action=RoutingAssignmentReceipt.ACTION_DIRECT_HANDOFF,
        )
        self.assertIsNotNone(receipt.consumed_at)

    def test_other_owner_returns_do_not_preempt_selected_task(self):
        """PLS-568: Sara/Ichi returns stay off Jun's focus projection."""
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            organization_id=1,
            team_name='engineering',
            defaults={
                'tiers': [[
                    'jun@shizuha.com',
                    'sara@shizuha.com',
                ]],
            },
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            organization_id=1,
            team_name='devops',
            defaults={'tiers': [['ichi@shizuha.com']]},
        )
        jun = self.user_model.objects.create_user(
            username='jun_pls568',
            email='jun@shizuha.com',
            password='testpass123',
        )
        selected = Item.objects.create(
            project=self.project,
            title='Jun selected ordinary task A',
            mode='task',
            status='in_progress',
            workflow_name='autonomous-bug',
            assignment_group='engineering',
            assignee_id=jun.id,
            assignee_email=jun.email,
            created_by_id=jun.id,
            created_by_email=jun.email,
        )
        WorkflowService.record_owner_queue_snapshot(jun.email, [selected.pk])
        transition = SimpleNamespace(
            id='return-other-owners',
            name='Return for Changes',
            post_functions=[{'type': 'assign_team', 'value': 'engineering'}],
        )
        owners = (
            ('Sara', 4, 'sara@shizuha.com', 'sara2574'),
            ('Ichi', 5, 'ichi@shizuha.com', 'ichi'),
        )
        identities = {
            login: {'id': uid, 'username': login.rstrip('2574'), 'email': email}
            for name, uid, email, login in owners
        }
        identities_by_id = {row['id']: row for row in identities.values()}

        def forgejo_response(url, **_kwargs):
            login = next(
                login for name, uid, email, login in owners
                if f'repo-{name.lower()}' in url
            )
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {'user': {'login': login}},
            )

        for name, uid, email, login in owners:
            root = Item.objects.create(
                project=self.project,
                title=f'{name}-owned returned root',
                mode='task',
                status='in_progress',
                priority='urgent',
                workflow_name='autonomous-bug',
                assignment_group='review',
                created_by_id=self.user.id,
                created_by_email=self.user.email,
                metadata={
                    'pull_requests': [{
                        'url': (
                            'https://origin.shizuha.com/shizuha-labs/'
                            f'repo-{name.lower()}/pulls/{uid}'
                        ),
                        'status': 'open',
                        'head_sha': f'head-{uid}',
                    }],
                },
            )
            create_blocked_item(
                blocker=root,
                project=self.project,
                title=f'{name} blocked dependent',
                mode='task',
                status='blocked',
                priority='urgent',
                workflow_name='autonomous-bug',
            )
            with patch('requests.get', side_effect=forgejo_response), patch(
                'tasks.user_service.user_service.get_user_by_username',
                side_effect=lambda username: identities.get(username),
            ), patch(
                'tasks.user_service.user_service.get_user_by_id',
                side_effect=lambda user_id: identities_by_id.get(user_id),
            ), patch(
                'tasks.services.admin_service.admin_service.get_organization',
                return_value={'id': 1, 'slug': 'shizuha'},
            ), patch.object(
                WorkflowService,
                '_is_agent_available_for_assignment',
                return_value=True,
            ), patch.dict(os.environ, {
                'FORGEJO_TOKEN': 'test-token',
                'ORIGIN_URL': 'https://origin.shizuha.com',
            }, clear=False):
                WorkflowService.execute_post_functions(transition, root)
            root.refresh_from_db()
            self.assertEqual(root.assignee_email, email)
            self.assertEqual(root.assignee_id, uid)
            self.assertIsNone(root.assignment_group)

        self.assertIsNone(
            WorkflowService._higher_impact_owned_blocker(selected, jun.email)
        )
        self.client.force_login(jun)
        response = self.client.post(
            '/api/comments/',
            {'item': selected.pk, 'content': 'A can still submit'},
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(
            WorkflowService.validate_transition(selected, 'in_progress', user=jun)
        )

    def test_origin_return_owner_reads_forgejo_author_identity(self):
        """PLS-568: a cross-team PR author is authorized by exact task org."""
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            organization_id=1,
            team_name='devops',
            defaults={'tiers': [['ichi@shizuha.com']]},
        )
        url = (
            'https://origin.shizuha.com/shizuha-labs/'
            'shizuha-tasks/pulls/568'
        )
        item = Item.objects.create(
            project=self.project,
            title='Origin author lookup',
            mode='task',
            status='in_progress',
            workflow_name='autonomous-bug',
            assignment_group='',
            assignee_id=None,
            assignee_email='',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
            metadata={
                'pull_requests': [{
                    'url': url,
                    'status': 'open',
                    'head_sha': uuid.uuid4().hex,
                }],
            },
        )
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {'user': {'login': 'ichi'}},
        )

        with patch('requests.get', return_value=response) as get, patch(
            'tasks.user_service.user_service.get_user_by_username',
            return_value={
                'id': 5,
                'username': 'ichi',
                'email': 'ichi@shizuha.com',
            },
        ), patch(
            'tasks.services.admin_service.admin_service.get_organization',
            return_value={'id': 1, 'slug': 'shizuha'},
        ), patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'test-token',
            'ORIGIN_URL': 'https://origin.shizuha.com',
        }, clear=False):
            decision = WorkflowService._origin_branch_owner_for_return(
                item, 'engineering',
            )

        self.assertEqual(decision['state'], 'branch_owner')
        self.assertEqual(decision['owner_id'], 5)
        self.assertEqual(decision['owner_email'], 'ichi@shizuha.com')
        self.assertEqual(decision['author_logins'], ['ichi'])
        self.assertEqual(decision['authorization']['task_org'], 'shizuha')
        self.assertIn(
            '/api/v1/repos/shizuha-labs/shizuha-tasks/pulls/568',
            get.call_args.args[0],
        )
        self.assertEqual(
            get.call_args.kwargs['headers']['Authorization'],
            'token test-token',
        )

    def test_unauthorized_return_owner_stays_on_typed_group_queue(self):
        """PLS-568: foreign branch ownership cannot fall back to a random seat."""
        TeamRoster.objects.create(
            organization_slug='foreign-org',
            organization_id=2,
            team_name='engineering',
            tiers=[['outsider@shizuha.com']],
        )
        item = Item.objects.create(
            project=self.project,
            title='Origin branch owner temporarily unavailable',
            mode='task',
            status='in_progress',
            workflow_name='autonomous-bug',
            assignment_group='review',
            assignee_id=None,
            assignee_email='',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
            metadata={
                'pull_requests': [{
                    'url': (
                        'https://origin.shizuha.com/shizuha-labs/'
                        'foreign-repo/pulls/9'
                    ),
                    'status': 'open',
                    'head_sha': uuid.uuid4().hex,
                }],
            },
        )
        transition = SimpleNamespace(
            id='return-group-hold',
            name='Return for Changes',
            post_functions=[{'type': 'assign_team', 'value': 'engineering'}],
        )
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {'user': {'login': 'outsider'}},
        )
        canonical_outsider = {
            'id': 41,
            'username': 'outsider',
            'email': 'outsider@shizuha.com',
        }
        with patch('requests.get', return_value=response), patch(
            'tasks.user_service.user_service.get_user_by_username',
            return_value=canonical_outsider,
        ), patch(
            'tasks.services.admin_service.admin_service.get_organization',
            return_value={'id': 1, 'slug': 'shizuha'},
        ), patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=False,
        ), patch.object(
            WorkflowService,
            '_resolve_team_assignee',
        ) as least_load, patch.dict(os.environ, {
            'FORGEJO_TOKEN': 'test-token',
            'ORIGIN_URL': 'https://origin.shizuha.com',
        }, clear=False):
            WorkflowService.execute_post_functions(transition, item)

        item.refresh_from_db()
        least_load.assert_not_called()
        self.assertIsNone(item.assignee_id)
        self.assertIn(item.assignee_email, ('', None))
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertEqual(
            item.metadata['branch_return_routing']['reason_code'],
            'origin_author_outside_task_org',
        )
        with patch.object(WorkflowService, '_resolve_team_assignee') as resolver:
            WorkflowService._ensure_assigned_if_working(item)
        resolver.assert_not_called()

    def test_return_transition_status_and_owner_route_share_one_transaction(self):
        """PLS-568: focus projection cannot observe half of a branch return."""
        in_review = Status.objects.get_or_create(
            slug='pls_568_in_review',
            defaults={'name': 'PLS 568 In Review', 'category': 'in_progress'},
        )[0]
        in_progress = Status.objects.get_or_create(
            slug='pls_568_in_progress',
            defaults={'name': 'PLS 568 In Progress', 'category': 'in_progress'},
        )[0]
        workflow = Workflow.objects.create(
            name='PLS 568 atomic return',
            slug='pls-568-atomic-return',
            is_active=True,
            initial_status=in_review,
        )
        WorkflowStatus.objects.create(
            workflow=workflow,
            status=in_review,
            position=1,
        )
        WorkflowStatus.objects.create(
            workflow=workflow,
            status=in_progress,
            position=2,
        )
        transition = WorkflowTransition.objects.create(
            workflow=workflow,
            name='Return for Changes',
            from_status=in_review.slug,
            to_status=in_progress.slug,
            post_functions=[{'type': 'assign_team', 'value': 'engineering'}],
        )
        item = Item.objects.create(
            project=self.project,
            title='Atomic return item',
            mode='task',
            status=in_review.slug,
            workflow_status=in_review.slug,
            workflow_name=workflow.slug,
            assignment_group='',
            assignee_id=None,
            assignee_email='',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )

        def assert_atomic(target, *_args, **_kwargs):
            self.assertTrue(connection.in_atomic_block)
            return target

        with patch.object(
            WorkflowService,
            '_execute_transition_impl',
            side_effect=assert_atomic,
        ):
            result = WorkflowService._execute_transition_with_dependency_lock(
                item,
                in_progress.slug,
                transition_id=transition.id,
            )

        self.assertEqual(result.pk, item.pk)

    def test_role_gating_with_project_admin_membership_for_security_alias(self):
        """A seeded admin project membership still unlocks security-role aliases."""
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user.id,
            user_email=self.user.email,
            role="admin",
            is_active=True,
        )
        transition = self._transition(["security", "security-lead"])
        item = self._item()
        self.assertTrue(
            WorkflowService._check_transition_conditions(
                transition,
                item,
                self.user,
            )
        )

    def test_role_gating_denies_org_role_for_different_org(self):
        """Project-scoped transitions ignore org-role grants from other orgs."""
        transition = self._transition(["admin"])
        item = self._item()
        self.user.organization_memberships = {2: "admin"}

        self.assertFalse(
            WorkflowService._check_transition_conditions(
                transition,
                item,
                self.user,
            )
        )

    def test_role_gating_denies_non_matching_project_role(self):
        """Matching membership on a different role must still be denied."""
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user.id,
            user_email=self.user.email,
            role="viewer",
            is_active=True,
        )
        transition = self._transition(["admin"])
        item = self._item()
        self.assertFalse(
            WorkflowService._check_transition_conditions(
                transition,
                item,
                self.user,
            )
        )

    def test_role_gating_resolves_org_roles_for_projectless_items(self):
        """Project-less transitions resolve through org-level roles."""
        transition = self._transition(["admin"])
        item = SimpleNamespace(
            project=None,
            assignee_id=self.user.id,
            assignee_email=self.user.email,
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )

        # Organization roles are read from JWT claim metadata and must match
        # allowed roles for project-less transitions.
        self.user.organization_memberships = {1: "member"}
        self.assertFalse(
            WorkflowService._check_transition_conditions(
                transition,
                item,
                self.user,
            )
        )

        self.user.organization_memberships = {1: "admin"}
        self.assertTrue(
            WorkflowService._check_transition_conditions(
                transition,
                item,
                self.user,
            )
        )

    def test_role_gating_allows_org_owner_for_allowed_admin(self):
        """Org owners should satisfy admin-equivalent project-less role checks."""
        transition = self._transition(["admin"])
        item = SimpleNamespace(
            project=None,
            assignee_id=self.user.id,
            assignee_email=self.user.email,
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )
        self.user.organization_memberships = {1: "owner"}

        self.assertTrue(
            WorkflowService._check_transition_conditions(
                transition,
                item,
                self.user,
            )
        )


class SameStatusTransitionActionTests(TestCase):
    """PLS-644: an explicit same-status transition is an action, not a no-op."""

    def setUp(self):
        binding_env = patch.dict(os.environ, {
            'PULSE_ROUTING_BINDINGS_ENFORCE': '1',
            'PULSE_ROUTING_BINDINGS_TEAMS': 'devops,architecture',
        }, clear=False)
        binding_env.start()
        self.addCleanup(binding_env.stop)
        user_model = get_user_model()
        self.author = user_model.objects.create_user(
            username='pls644-author',
            email='pls644-author@example.com',
            password='testpass123',
            is_staff=True,
        )
        self.devops = user_model.objects.create_user(
            username='pls644-devops',
            email='ichi@shizuha.com',
            password='testpass123',
        )
        self.aoi = user_model.objects.create_user(
            username='pls644-aoi',
            email='aoi@shizuha.com',
            password='testpass123',
        )
        self.project = Project.objects.create(
            name='PLS 644 same-status routing',
            key='PSS',
            org_id=4,
            access_level='open',
        )
        TeamRoster.objects.update_or_create(
            organization_slug='personal-3',
            organization_id=self.project.org_id,
            team_name='architecture',
            defaults={'tiers': [[self.author.email]], 'is_active': True},
        )
        self.todo = Status.objects.create(
            slug='pls_644_todo',
            name='PLS 644 To Do',
            category='todo',
        )
        self.workflow = Workflow.objects.create(
            name='PLS 644 workflow',
            slug='pls-644-workflow',
            initial_status=self.todo,
            is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow,
            status=self.todo,
            position=0,
        )
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Escalate to DevOps',
            from_status=self.todo.slug,
            to_status=self.todo.slug,
            is_global=True,
            post_functions=[{'type': 'assign_team', 'value': 'devops'}],
        )
        self.client = APIClient()
        self.client.force_authenticate(self.author)
        self._seed_binding('devops', self.devops)

    def _seed_binding(self, team_name, user):
        """Materialize the real exact-org resolver boundary for one team seat."""
        team = RoutingTeam.objects.create(
            organization_id=self.project.org_id,
            key=team_name,
            display_name=team_name.title(),
            state='active',
            version=1,
            digest=uuid.uuid4().hex * 2,
        )
        now = timezone.now()
        subject = RoutingSubject.objects.create(
            organization_id=self.project.org_id,
            subject_id=user.id,
            canonical_email=user.email,
            state='active',
            generation=1,
            digest=uuid.uuid4().hex * 2,
            observed_at=now,
            lease_until=now + timedelta(seconds=90),
        )
        RoutingAuthorityCheckpoint.objects.create(
            authority_id='shizuha-id',
            aggregate_key=f'{self.project.org_id}:{user.id}',
            subject_generation=1,
            observed_version=1,
            digest=subject.digest,
            revocation_high_water=0,
            checkpoint_generation=1,
            source_event_id=uuid.uuid4(),
            first_received_at=now,
            source_expires_at=now + timedelta(seconds=90),
            observed_at=now,
            lease_until=now + timedelta(seconds=90),
        )
        RoutingTeamBinding.objects.create(
            organization_id=self.project.org_id,
            routing_team=team,
            subject=subject,
            state='active',
            version=1,
            is_current=True,
            subject_generation=1,
            command_id=uuid.uuid4(),
            digest=uuid.uuid4().hex * 2,
        )
        ReceiveAssignmentEntitlement.objects.create(
            organization_id=self.project.org_id,
            subject=subject,
            routing_team=team,
            project=None,
            workflow_name=self.workflow.slug,
            action='receive_assignment',
            state='active',
            version=1,
            is_current=True,
            subject_generation=1,
            grant_source='reviewed_task:PLS-644',
            grant_issuer_id=self.author.id,
            grant_issuer_email=self.author.email,
            grant_event_id=uuid.uuid4(),
            command_id=uuid.uuid4(),
            digest=uuid.uuid4().hex * 2,
        )

    def _item(self, *, assignment_group='architecture', assignee=None):
        assignee = assignee or self.author
        item = Item.objects.create(
            project=self.project,
            title='Cross-org blocker needs a durable reroute',
            mode='task',
            status=self.todo.slug,
            workflow_status=self.todo.slug,
            workflow_name=self.workflow.slug,
            created_by_id=self.author.id,
            created_by_email=self.author.email,
        )
        # Reproduce the already-persisted source tuple.  In particular, the
        # no-op case starts on an armed exact-authority lane; constructing that
        # historical row through a new direct save would correctly be rejected
        # by the one-use routing receipt fence before the transition under test.
        Item.objects.filter(pk=item.pk).update(
            assignment_group=assignment_group,
            assignee_id=assignee.id,
            assignee_email=assignee.email,
        )
        item.refresh_from_db()
        return item

    def test_api_cross_org_reject_escalate_fresh_read_and_repair_sequence(self):
        """Production order: rejected handoff -> reroute -> read -> repaired handoff."""
        item = self._item()

        def resolve_user(*, user_id=None, email=None):
            user = next(
                candidate for candidate in (self.author, self.devops, self.aoi)
                if candidate.id == user_id or candidate.email == email
            )
            return {
                'id': user.id,
                'email': user.email,
                'username': user.username,
                'is_agent': True,
            }

        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=False,
        ), patch(
            'tasks.views.user_service.resolve_user_info',
            side_effect=resolve_user,
        ), patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), patch.object(
            WorkflowService,
            '_member_can_execute_from_status',
            return_value=True,
        ), patch.object(
            WorkflowService,
            '_seat_satisfies_required_capability',
            return_value=True,
        ):
            from tasks.services.routing_authority import (
                subject_authorized_for_item,
            )
            authority = subject_authorized_for_item(
                self.devops.id,
                item,
                team_name='devops',
                expected_assignment_group='devops',
            )
            self.assertTrue(authority.allowed, authority)
            verdict = WorkflowService._seat_routability_verdict(
                item,
                'devops',
                self.devops.email,
                user_id=self.devops.id,
            )
            self.assertTrue(verdict.ok, verdict)
            self.assertEqual(
                WorkflowService._resolve_team_assignee(
                    'devops',
                    exclude_email=item.created_by_email,
                    project_key=item.project.key,
                    item=item,
                    assignment_group_override='devops',
                ),
                (self.devops.id, self.devops.email),
            )
            rejected = self.client.post(
                f'/api/items/{item.id}/assign/',
                {
                    'assignee_id': self.aoi.id,
                    'assignee_email': self.aoi.email,
                    'direct_assignment_reason': 'PLAT-999 named co-signer',
                },
                format='json',
            )
            self.assertEqual(rejected.status_code, 400, rejected.data)

            escalated = self.client.post(
                f'/api/items/{item.id}/execute-transition/',
                {
                    'to_status': self.todo.slug,
                    'transition_id': str(self.transition.id),
                },
                format='json',
            )
            self.assertEqual(escalated.status_code, 200, escalated.data)

            fresh = self.client.get(f'/api/items/{item.id}/')
            self.assertEqual(fresh.status_code, 200, fresh.data)
            self.assertEqual(fresh.data['status'], self.todo.slug)
            self.assertEqual(fresh.data['assignment_group'], 'devops')
            self.assertEqual(fresh.data['assignee_email'], self.devops.email)

            # The architecture seat is now materialized in the task org.  The
            # same top-level assignment API reroutes through the exact-org
            # resolver (rather than a mocked _resolve_team_assignee) and lands
            # the named co-signer.
            self._seed_binding('architecture', self.aoi)
            repaired = self.client.post(
                f'/api/items/{item.id}/assign/',
                {'assignment_group': 'architecture'},
                format='json',
            )
            self.assertEqual(repaired.status_code, 200, repaired.data)

            repaired_fresh = self.client.get(f'/api/items/{item.id}/')
            self.assertEqual(repaired_fresh.status_code, 200, repaired_fresh.data)
            self.assertEqual(
                repaired_fresh.data['assignment_group'], 'architecture',
            )
            self.assertEqual(repaired_fresh.data['assignee_email'], self.aoi.email)

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
    @patch.object(
        WorkflowService,
        '_seat_satisfies_required_capability',
        return_value=True,
    )
    def test_api_rejects_genuine_same_status_routing_noop_and_rolls_back(self, *_):
        item = self._item()

        first = self.client.post(
            f'/api/items/{item.id}/execute-transition/',
            {
                'to_status': self.todo.slug,
                'transition_id': str(self.transition.id),
            },
            format='json',
        )
        self.assertEqual(first.status_code, 200, first.data)

        response = self.client.post(
            f'/api/items/{item.id}/execute-transition/',
            {
                'to_status': self.todo.slug,
                'transition_id': str(self.transition.id),
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data['code'], 'SAME_STATUS_ROUTING_NOOP')
        item.refresh_from_db()
        self.assertEqual(item.status, self.todo.slug)
        self.assertEqual(item.assignment_group, 'devops')
        self.assertEqual(item.assignee_id, self.devops.id)
        self.assertEqual(item.assignee_email, self.devops.email)

    def test_same_status_call_without_action_id_remains_idempotent_noop(self):
        item = self._item()

        with patch.object(WorkflowService, '_resolve_team_assignee') as resolver:
            WorkflowService.execute_transition(
                item,
                self.todo.slug,
                user=self.author,
            )

        item.refresh_from_db()
        self.assertEqual(item.assignment_group, 'architecture')
        self.assertEqual(item.assignee_email, self.author.email)
        resolver.assert_not_called()

    def test_explicit_same_status_post_function_failure_rolls_back(self):
        item = self._item()

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=RuntimeError('injected routing failure'),
        ), self.assertRaisesRegex(RuntimeError, 'injected routing failure'):
            WorkflowService.execute_transition(
                item,
                self.todo.slug,
                user=self.author,
                transition_id=str(self.transition.id),
            )

        item.refresh_from_db()
        self.assertEqual(item.status, self.todo.slug)
        self.assertEqual(item.assignment_group, 'architecture')
        self.assertEqual(item.assignee_id, self.author.id)
        self.assertEqual(item.assignee_email, self.author.email)


class ExecutePostFunctionsHardeningTests(TestCase):
    """PLAT-519: execute_post_functions must coerce string-encoded post_functions.

    Regression guard: if post_functions is stored as a JSON string (instead of
    a parsed list), the executor must not silently no-op by iterating characters.
    """

    def _item(self):
        ns = SimpleNamespace(
            assignee_id=42,
            assignee_email="old@example.com",
            project=None,
            created_by_id=1,
            created_by_email="creator@example.com",
            assignment_group=None,
        )
        # assign_to/unassign now delegates to item.unassign(save=False) rather
        # than mutating the fields directly, and execute_post_functions calls
        # item.save(update_fields=...) at the end — so the fake must provide both.
        def _unassign(save=False):
            ns.assignee_id = None
            ns.assignee_email = None
        ns.unassign = _unassign
        ns.save = lambda *a, **k: None
        return ns

    def test_string_encoded_post_functions_coerced_and_executed(self):
        """String-encoded post_functions are parsed and executed correctly."""
        pf_list = [{'type': 'assign_to', 'target': 'unassign'}]
        transition = SimpleNamespace(
            id='t-string',
            post_functions=json.dumps(pf_list),
        )
        item = self._item()

        WorkflowService.execute_post_functions(transition, item)

        # assign_to/unassign should have fired and cleared the assignee
        self.assertIsNone(item.assignee_id)
        self.assertIsNone(item.assignee_email)


    def test_list_post_functions_still_execute(self):
        """Normal list post_functions are unaffected by the PLAT-519 guard."""
        transition = SimpleNamespace(
            id='t-list',
            post_functions=[{'type': 'assign_to', 'target': 'unassign'}],
        )
        item = self._item()

        WorkflowService.execute_post_functions(transition, item)

        self.assertIsNone(item.assignee_id)
        self.assertIsNone(item.assignee_email)

    def test_malformed_string_post_functions_skips_without_raising(self):
        """A malformed JSON string in post_functions logs an error and skips gracefully."""
        transition = SimpleNamespace(
            id='t-bad-json',
            post_functions='this-is-not-json',
        )
        item = self._item()

        WorkflowService.execute_post_functions(transition, item)

        # Assignee unchanged — no post-function executed, no exception raised
        self.assertEqual(item.assignee_id, 42)
        self.assertEqual(item.assignee_email, "old@example.com")

    def test_assign_team_persists_canonical_alias_without_a_resolved_seat(self):
        project = Project.objects.create(
            org_id=1,
            name='Legacy Platform Ops Post Function',
            key='LPOPF',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='devops',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[]],
                'is_active': True,
            },
        )
        item = Item.objects.create(
            title='Legacy alias must not persist',
            mode='task',
            status='open',
            assignment_group='engineering',
            project=project,
        )
        transition = SimpleNamespace(
            id='legacy-platform-ops',
            post_functions=[
                {'type': 'assign_team', 'value': 'platform-ops'},
            ],
        )

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(None, None),
        ):
            WorkflowService.execute_post_functions(transition, item)

        item.refresh_from_db()
        self.assertEqual(item.assignment_group, 'devops')
        self.assertIsNone(item.assignee_id)
        self.assertFalse(item.assignee_email)

    def test_legacy_operator_post_function_routes_admin_ops_team(self):
        """Raise-to-Admin-Ops must not direct-assign the CEO via target=operator."""
        project = Project.objects.create(
            org_id=1,
            name='Legacy Admin Ops Reroute',
            key='LAOR',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='admin-ops',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['shizuha@shizuha.com']],
                'is_active': True,
            },
        )
        item = create_blocked_item(
            project=project,
            mode='task',
            title='Operator escalation should go through Admin Ops',
            workflow_name='simple',
            status='blocked',
            assignment_group='merge',
            assignee_id=11,
            assignee_email='sora@shizuha.com',
            created_by_id=12,
            created_by_email='rei@shizuha.com',
        )
        satisfy_blocked_invariant(item)
        transition = SimpleNamespace(
            id='t-operator',
            name='Raise to Admin Ops',
            post_functions=[{'type': 'assign_to', 'target': 'operator'}],
        )

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(24, 'shizuha@shizuha.com'),
        ):
            WorkflowService.execute_post_functions(transition, item)

        item.refresh_from_db()
        self.assertEqual(item.assignment_group, 'admin-ops')
        self.assertEqual(item.assignee_id, 24)
        self.assertEqual(item.assignee_email, 'shizuha@shizuha.com')

    def test_escalation_gate_reroutes_permission_gap_to_owning_team(self):
        """PLAT-1347: a 403/capability gap must route sideways, not to humans."""
        project = Project.objects.create(
            org_id=1,
            name='Capability Gap Reroute',
            key='CGR',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='devops',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['ichi@shizuha.com']],
                'is_active': True,
            },
        )
        item = create_blocked_item(
            project=project,
            mode='task',
            title='Add org member failed with admin_invite_member 403',
            # bug-workflow has both 'blocked' (initial here) and 'open' (the gate's
            # reroute target); 'simple' lacks 'open', so the gate's status='open'
            # would coerce to 'todo' on save.
            workflow_name='autonomous-dev',
            status='blocked',
            workflow_status='blocked',
            assignment_group='human-ops',
            assignee_id=1,
            assignee_email='operator@example.test',
            created_by_id=12,
            created_by_email='agent@example.com',
            description='Agent lacks permission to add an org member.',
        )
        transition = SimpleNamespace(
            id='t-human',
            name='Escalate to Human Ops',
            post_functions=[{'type': 'assign_team', 'value': 'human-ops'}],
        )

        with patch.object(WorkflowService, '_resolve_team_assignee', return_value=(15, 'ichi@shizuha.com')), \
             patch.object(WorkflowService, '_is_team_member', return_value=True):
            rerouted = WorkflowService._apply_escalation_routing_gate(
                item,
                transition,
                screen_data={'comment': 'admin_invite_member returned 403 permission denied; need JIT elevation'},
            )

        item.refresh_from_db()
        self.assertTrue(rerouted)
        self.assertEqual(item.assignment_group, 'devops')
        self.assertEqual(item.status, 'open')
        # workflow_status holds the status DISPLAY NAME (Item._sync_status_fields
        # keeps status=slug 'open' ↔ workflow_status=name 'Open' in sync).
        self.assertEqual(item.workflow_status, 'Open')
        self.assertEqual(item.assignee_email, 'ichi@shizuha.com')
        self.assertEqual(item.metadata['escalation_routing_gate']['classification'], 'capability_gap')
        self.assertEqual(item.metadata['escalation_routing_gate']['outcome'], 'rerouted')
        self.assertIn('invariant', item.metadata['escalation_routing_gate'])
        self.assertEqual(item.metadata['escalation_routing_gate']['liveness'], 'gate_executed')
        self.assertTrue(
            Comment.objects.filter(item=item, content__icontains='PLAT-1347 escalation gate rerouted').exists()
        )

    def test_escalation_gate_allows_true_spend_decision(self):
        """PLAT-1347: the five critical decision classes still pass through."""
        item = create_blocked_item(
            mode='task',
            title='Approve paid vendor plan upgrade',
            workflow_name='simple',
            status='blocked',
            workflow_status='blocked',
            assignment_group='admin-ops',
            assignee_id=24,
            assignee_email='shizuha@shizuha.com',
            created_by_id=12,
            created_by_email='agent@example.com',
            description='Need material spend approval for an external vendor paid plan.',
        )
        satisfy_blocked_invariant(item)
        transition = SimpleNamespace(
            id='t-admin',
            name='Raise to Admin Ops',
            post_functions=[{'type': 'assign_team', 'value': 'admin-ops'}],
        )

        rerouted = WorkflowService._apply_escalation_routing_gate(
            item,
            transition,
            screen_data={'comment': 'material spend decision needed'},
        )

        item.refresh_from_db()
        self.assertFalse(rerouted)
        self.assertEqual(item.assignment_group, 'admin-ops')
        self.assertEqual(item.status, 'blocked')
        self.assertEqual(item.metadata['escalation_routing_gate']['outcome'], 'allowed')
        self.assertIn('invariant', item.metadata['escalation_routing_gate'])
        self.assertEqual(item.metadata['escalation_routing_gate']['liveness'], 'gate_executed')
        self.assertTrue(
            Comment.objects.filter(item=item, content__icontains='allowed human/admin escalation').exists()
        )

    def test_operator_request_hygiene_rejects_epic_escalation(self):
        """PLAT-3136: umbrella/epic requests must stay off the operator queue."""
        item = create_blocked_item(
            mode='task',
            item_type='task.epic',
            title='[EPIC] Set up & maintain vendor compliance since inception',
            workflow_name='autonomous-dev',
            status='blocked',
            workflow_status='blocked',
            assignment_group='admin-ops',
            assignee_id=24,
            assignee_email='shizuha@shizuha.com',
            created_by_id=12,
            created_by_email='agent@example.com',
            description='Need legal contract approval for this whole program.',
        )
        transition = SimpleNamespace(
            id='t-admin',
            name='Raise to Admin Ops',
            post_functions=[{'type': 'assign_team', 'value': 'admin-ops'}],
        )

        with patch.object(WorkflowService, '_resolve_team_assignee', return_value=(15, 'eng@shizuha.com')), \
             patch.object(WorkflowService, '_is_team_member', return_value=True):
            rerouted = WorkflowService._apply_escalation_routing_gate(
                item,
                transition,
                screen_data={'comment': 'legal contract approval needed'},
            )

        item.refresh_from_db()
        self.assertTrue(rerouted)
        self.assertNotEqual(item.assignment_group, 'admin-ops')
        gate = item.metadata['escalation_routing_gate']
        self.assertEqual(gate['outcome'], 'rejected_hygiene')
        self.assertEqual(gate['classification'], 'operator_request_hygiene')
        self.assertIn('umbrella_epic', gate['hygiene_violations'])
        self.assertIn('umbrella_language', gate['hygiene_violations'])
        self.assertTrue(gate['atomic_operator_ask_required'])
        self.assertTrue(
            Comment.objects.filter(item=item, content__icontains='operator-request hygiene gate rejected').exists()
        )

    def test_operator_request_hygiene_allows_atomic_ask_even_when_task_description_has_bullets(self):
        """PLAT-3136: bundled detection must not scan normal task AC bullets."""
        item = create_blocked_item(
            mode='task',
            title='Vendor contract approval follow-up',
            workflow_name='autonomous-dev',
            status='blocked',
            workflow_status='blocked',
            assignment_group='admin-ops',
            assignee_id=24,
            assignee_email='shizuha@shizuha.com',
            created_by_id=12,
            created_by_email='agent@example.com',
            description=(
                'Context and acceptance criteria:\n'
                '- provide vendor summary to finance\n'
                '- verify audit trail after approval\n'
                '- confirm downstream task links'
            ),
        )
        satisfy_blocked_invariant(item)
        transition = SimpleNamespace(
            id='t-admin',
            name='Raise to Admin Ops',
            post_functions=[{'type': 'assign_team', 'value': 'admin-ops'}],
        )

        rerouted = WorkflowService._apply_escalation_routing_gate(
            item,
            transition,
            screen_data={'comment': 'Legal contract sign-off needed for the Acme DPA only.'},
        )

        item.refresh_from_db()
        self.assertFalse(rerouted)
        gate = item.metadata['escalation_routing_gate']
        self.assertEqual(gate['outcome'], 'allowed')
        self.assertEqual(gate['classification'], 'genuine_decision_or_physical_action')
        self.assertNotIn('hygiene_violations', gate)

    def test_operator_request_hygiene_rejects_open_ended_task_title_with_atomic_comment(self):
        """PLAT-3136: open-ended task titles must not bypass on atomic screen text."""
        item = create_blocked_item(
            mode='task',
            title='Approve any remaining vendor actions if available',
            workflow_name='autonomous-dev',
            status='blocked',
            workflow_status='blocked',
            assignment_group='admin-ops',
            assignee_id=24,
            assignee_email='shizuha@shizuha.com',
            created_by_id=12,
            created_by_email='agent@example.com',
            description='Context has no bundled operator asks.',
        )
        transition = SimpleNamespace(
            id='t-admin',
            name='Raise to Admin Ops',
            post_functions=[{'type': 'assign_team', 'value': 'admin-ops'}],
        )

        with patch.object(WorkflowService, '_resolve_team_assignee', return_value=(15, 'eng@shizuha.com')), \
             patch.object(WorkflowService, '_is_team_member', return_value=True):
            rerouted = WorkflowService._apply_escalation_routing_gate(
                item,
                transition,
                screen_data={'comment': 'Legal contract sign-off needed for the Acme DPA only.'},
            )

        item.refresh_from_db()
        self.assertTrue(rerouted)
        gate = item.metadata['escalation_routing_gate']
        self.assertEqual(gate['outcome'], 'rejected_hygiene')
        self.assertEqual(gate['classification'], 'operator_request_hygiene')
        self.assertIn('open_ended_ask', gate['hygiene_violations'])
        self.assertNotIn('bundled_multiple_asks', gate['hygiene_violations'])

    def test_operator_request_hygiene_rejects_bundled_and_open_ended_ask(self):
        """PLAT-3136: one operator-bound task must contain exactly one concrete ask."""
        item = create_blocked_item(
            mode='task',
            title='Approve remaining vendor actions if available',
            workflow_name='autonomous-dev',
            status='blocked',
            workflow_status='blocked',
            assignment_group='admin-ops',
            assignee_id=24,
            assignee_email='shizuha@shizuha.com',
            created_by_id=12,
            created_by_email='agent@example.com',
            description='Please approve the paid plan, confirm the legal terms, and verify any remaining access.',
        )
        transition = SimpleNamespace(
            id='t-admin',
            name='Raise to Admin Ops',
            post_functions=[{'type': 'assign_team', 'value': 'admin-ops'}],
        )

        with patch.object(WorkflowService, '_resolve_team_assignee', return_value=(15, 'eng@shizuha.com')), \
             patch.object(WorkflowService, '_is_team_member', return_value=True):
            rerouted = WorkflowService._apply_escalation_routing_gate(
                item,
                transition,
                screen_data={'comment': 'if available, approve and confirm'},
            )

        item.refresh_from_db()
        self.assertTrue(rerouted)
        gate = item.metadata['escalation_routing_gate']
        self.assertEqual(gate['outcome'], 'rejected_hygiene')
        self.assertIn('bundled_multiple_asks', gate['hygiene_violations'])
        self.assertIn('open_ended_ask', gate['hygiene_violations'])

    def test_pls1195_typed_evidence_subject_is_not_bundled_from_comment_or_title(self):
        """PLS-1195: hygiene classifies on dependency.requested_subject, not task text."""
        item = create_blocked_item(
            mode='task',
            title='Legal contract sign-off for the Acme DPA',
            workflow_name='autonomous-dev',
            status='in_progress',
            workflow_status='in_progress',
            assignment_group='engineering',
            assignee_id=24,
            assignee_email='nagi@agents.shizuha.io',
            created_by_id=12,
            created_by_email='agent@example.com',
            description='Please restore, merge, and verify any remaining access.',
        )
        transition = SimpleNamespace(
            id='t-admin',
            name='Raise to Admin Ops',
            post_functions=[{'type': 'assign_team', 'value': 'admin-ops'}],
        )
        evidence = {
            'schema': 'shizuha.pulse.escalation-evidence.v1',
            'request_id': str(uuid.uuid4()),
            'expected_generation': 0,
            'dependency': {
                'evidence_class': 'human_held_source_evidence',
                'requested_kind': 'artifact',
                'requested_subject': 'Restore shizuha-computax runner reachability from books pods',
                'holder_type': 'operator',
                'holder_ref': 'operator',
                'acceptance_check': 'books pods no longer 503 on the computax runner',
                'source_refs': [{'kind': 'task', 'id': item.item_key}],
            },
            'technical_action': None,
        }

        with patch.object(WorkflowService, '_resolve_team_assignee', return_value=(15, 'eng@shizuha.com')), \
             patch.object(WorkflowService, '_is_team_member', return_value=True):
            rerouted = WorkflowService._apply_escalation_routing_gate(
                item,
                transition,
                screen_data={
                    'comment': 'restore runner and merge seeding then verify',
                    'escalation_evidence': evidence,
                },
            )

        item.refresh_from_db()
        self.assertFalse(rerouted)
        gate = (item.metadata or {}).get('escalation_routing_gate') or {}
        self.assertNotEqual(gate.get('outcome'), 'rejected_hygiene')

    def test_escalation_gate_fails_loud_on_classifier_exception(self):
        """PLAT-1254/1347: gate failure leaves durable ANDON + liveness audit."""
        project = Project.objects.create(
            org_id=1,
            name='Escalation Gate ANDON',
            key='EGAN',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='devops',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['ichi@shizuha.com']],
                'is_active': True,
            },
        )
        item = create_blocked_item(
            project=project,
            mode='task',
            title='Ambiguous escalation should fail loud if gate errors',
            # bug-workflow has both 'blocked' and 'open'; 'simple' lacks 'open'.
            workflow_name='autonomous-dev',
            status='blocked',
            workflow_status='blocked',
            assignment_group='human-ops',
            assignee_id=1,
            assignee_email='operator@example.test',
            created_by_id=12,
            created_by_email='agent@example.com',
            description='Need help deciding where this goes.',
        )
        transition = SimpleNamespace(
            id='t-human',
            name='Escalate to Human Ops',
            post_functions=[{'type': 'assign_team', 'value': 'human-ops'}],
        )

        with patch.object(
            WorkflowService,
            '_escalation_gate_is_genuine_human_decision',
            side_effect=RuntimeError('classifier unavailable'),
        ), patch.object(
            WorkflowService, '_resolve_team_assignee',
            return_value=(15, 'ichi@shizuha.com'),
        ), patch.object(WorkflowService, '_is_team_member', return_value=True):
            rerouted = WorkflowService._apply_escalation_routing_gate(
                item,
                transition,
                screen_data={'comment': 'please escalate'},
            )

        item.refresh_from_db()
        self.assertTrue(rerouted)
        self.assertEqual(item.assignment_group, 'devops')
        self.assertEqual(item.status, 'open')
        self.assertEqual(item.assignee_email, 'ichi@shizuha.com')
        gate = item.metadata['escalation_routing_gate']
        self.assertEqual(gate['outcome'], 'fail_loud')
        self.assertEqual(gate['classification'], 'gate_failure')
        self.assertEqual(gate['failure_path'], 'durable_pulse_andon_comment_to_devops_owner')
        self.assertIn('invariant', gate)
        self.assertEqual(gate['liveness'], 'gate_executed')
        self.assertTrue(
            Comment.objects.filter(
                item=item,
                content__icontains='ANDON — PLAT-1347 escalation-routing gate failed loud',
            ).exists()
        )



class TypedEscalationRoutingTests(TestCase):
    """PLAT-4609: typed evidence is the only operator-routing authority."""

    def setUp(self):
        self.user_model = get_user_model()
        self.user = self.user_model.objects.create_user(
            username='typed_actor', email='typed_actor@example.com', password='pw',
            is_staff=True,
        )
        self.other = self.user_model.objects.create_user(
            username='typed_other', email='typed_other@example.com', password='pw',
            is_staff=True,
        )
        self.project = Project.objects.create(
            name='Typed escalation project', key='TER', org_id=1,
        )
        self.open_status, _ = Status.objects.get_or_create(
            slug='open', defaults={'name': 'Open', 'category': 'todo'},
        )
        self.blocked_status, _ = Status.objects.get_or_create(
            slug='blocked', defaults={'name': 'Blocked', 'category': 'in_progress'},
        )
        self.workflow = Workflow.objects.create(
            name='Typed Escalation', slug='typed-escalation',
            initial_status=self.open_status, is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow, status=self.open_status, position=0,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow, status=self.blocked_status, position=1,
        )
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Raise to Admin Ops',
            from_status='open',
            to_status='blocked',
            is_global=True,
            # This is the regression trap: the typed service must never execute
            # this legacy prose-era routing post-function.
            post_functions=[
                {
                    'type': 'create_blocker_task',
                    'team': 'admin-ops',
                    'workflow': 'simple',
                    'priority': 'urgent',
                    'source': 'PLAT-4215',
                },
                {'type': 'assign_team', 'value': 'devops'},
            ],
        )
        for team_name, email in (
            ('admin-ops', 'shion@shizuha.com'),
            ('ceo-decisions', 'operator@example.test'),
            ('human-ops', 'operator@example.com'),
            ('devops', 'san@shizuha.com'),
        ):
            TeamRoster.objects.update_or_create(
                organization_id=1,
                team_name=team_name,
                defaults={
                    'organization_slug': 'shizuha',
                    'tiers': [[email]],
                    'is_active': True,
                },
            )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.user.email, 'kai@shizuha.com']],
                'is_active': True,
            },
        )

    def _item(self, *, title='Operator-held statutory evidence', group='engineering'):
        return Item.objects.create(
            mode='task', item_type='task.bug', title=title,
            project=self.project, item_key=self.project.get_next_item_key(),
            workflow_name=self.workflow.slug, status='open', workflow_status='Open',
            assignment_group=group, assignee_id=self.user.id,
            assignee_email=self.user.email, created_by_id=self.user.id,
            created_by_email=self.user.email,
        )

    def _evidence(self, item, *, klass='human_held_source_evidence', holder='external_authority',
                  kind='fact', holder_ref='gst-portal', subject='GST registration status',
                  expected=0, request_id=None, technical_action=None):
        return {
            'schema': escalation_routing.SCHEMA,
            'request_id': request_id or str(uuid.uuid4()),
            'expected_generation': expected,
            'dependency': {
                'evidence_class': klass,
                'requested_kind': kind,
                'requested_subject': subject,
                'holder_type': holder,
                'holder_ref': holder_ref,
                'acceptance_check': 'Record the authoritative non-secret result.',
                'source_refs': [{'kind': 'task', 'id': item.item_key}],
            },
            'technical_action': technical_action,
        }

    @staticmethod
    def _resolver(group, **_kwargs):
        rows = {
            'admin-ops': (201, 'shion@shizuha.com'),
            'ceo-decisions': (3, 'operator@example.test'),
            'human-ops': (202, 'operator@example.com'),
            'devops': (203, 'san@shizuha.com'),
            'engineering': (204, 'kai@shizuha.com'),
        }
        return rows.get(group, (None, None))

    @staticmethod
    def _apply(item, uid, email, update_fields, **_kwargs):
        item.assignee_id = uid
        item.assignee_email = email
        for field in ('assignee_id', 'assignee_email'):
            if field not in update_fields:
                update_fields.append(field)
        return uid, email

    def _patches(self):
        return (
            patch.object(WorkflowService, '_resolve_team_assignee', side_effect=self._resolver),
            patch.object(WorkflowService, '_apply_resolved_assignee', side_effect=self._apply),
            patch.object(escalation_routing, '_resolve_catalog', side_effect=lambda _item, ref: (ref, 7)),
        )

    def _execute(self, item, evidence, *, user=None):
        p1, p2, p3 = self._patches()
        with p1, p2, p3:
            return WorkflowService.execute_transition(
                item, 'blocked', user=user or self.user,
                screen_data={
                    'comment': 'One atomic statutory ask.',
                    'escalation_evidence': evidence,
                },
                transition_id=self.transition.id,
            )

    def test_bksg20_mixed_words_preserve_operator_statutory_lane(self):
        item = self._item(title='GST portal OTP DSC access and statutory status')
        evidence = self._evidence(
            item,
            subject='GST registration, GSTIN, filing cadence, portal OTP and DSC access',
        )

        result = self._execute(item, evidence)

        result.refresh_from_db()
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.assignment_group, 'admin-ops')
        self.assertEqual(result.assignee_email, 'shion@shizuha.com')
        self.assertNotEqual(result.assignment_group, 'devops')
        receipt = EscalationRoutingReceipt.objects.get(item=result)
        self.assertEqual(receipt.classification, 'operator_held_dependency')
        self.assertEqual(receipt.final_state['assignment_group'], 'admin-ops')
        self.assertEqual(EscalationRoutingDisposition.objects.filter(item=result).count(), 1)
        self.assertEqual(EscalationRoutingNotificationOutbox.objects.filter(item=result).count(), 1)
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        blocker = disposition.child_item
        blocker.refresh_from_db()
        self.assertEqual(blocker.status, 'todo')
        self.assertFalse(WorkflowService._status_is_terminal(blocker.status))
        self.assertNotIn(
            blocker.status,
            WorkflowService.BLOCK_RELEASING_NONTERMINAL_SLUGS,
        )
        self.assertTrue(WorkflowService._has_active_assigned_blocker(result))

    def test_correct_human_escalation_leaves_start_and_comment_surfaces_admissible(self):
        parent = self._item(title='Operator-held filing confirmation')
        result = self._execute(parent, self._evidence(parent))
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        disposition.child_item.refresh_from_db()
        self.assertEqual(disposition.child_item.status, 'todo')

        in_progress, _ = Status.objects.get_or_create(
            slug='in_progress',
            defaults={'name': 'In Progress', 'category': 'in_progress'},
        )
        WorkflowStatus.objects.get_or_create(
            workflow=self.workflow,
            status=in_progress,
            defaults={'position': 1},
        )
        start = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Start Progress',
            from_status='open',
            to_status='in_progress',
        )
        unrelated = self._item(title='Unrelated engineering task')
        client = APIClient()
        client.force_authenticate(self.user)

        comment = client.post(
            '/api/comments/',
            {'item': unrelated.id, 'content': 'Starting the unrelated work.'},
            format='json',
        )
        self.assertEqual(comment.status_code, 201, comment.data)
        transition = client.post(
            f'/api/items/{unrelated.id}/execute-transition/',
            {'to_status': 'in_progress', 'transition_id': str(start.id)},
            format='json',
        )
        self.assertEqual(transition.status_code, 200, transition.data)
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.status, 'in_progress')

    def test_valid_forced_hold_preserves_evidence_and_parks_the_child(self):
        item = self._item(title='Operator-held ask with an unavailable route')
        evidence = self._evidence(item)

        p1, p2, p3 = self._patches()
        with p1, p2, p3:
            result = escalation_routing.EscalationRoutingService._execute_forced_hold(
                item.pk,
                self.transition,
                user=self.user,
                screen_data={
                    'comment': 'One atomic statutory ask.',
                    'escalation_evidence': evidence,
                },
                reason_code='wip_reservation_target_mismatch',
            )

        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(
            disposition.reason_code, 'wip_reservation_target_mismatch'
        )
        self.assertEqual(
            disposition.normalized_evidence['dependency']['holder_ref'],
            'gst-portal',
        )
        disposition.child_item.refresh_from_db()
        self.assertEqual(disposition.child_item.status, 'todo')

    def test_unresolved_operator_destination_parks_the_carrier(self):
        """PLS-278: a valid human ask with no exact-org seat must leave the ready set."""
        Status.objects.get_or_create(
            slug='waiting_external',
            defaults={'name': 'Waiting External', 'category': 'todo'},
        )
        item = self._item(title='Operator-held source trees with no exact-org lane')
        evidence = self._evidence(item)

        p1, p2, p3 = self._patches()
        with p1, p2, p3:
            result = escalation_routing.EscalationRoutingService._execute_forced_hold(
                item.pk,
                self.transition,
                user=self.user,
                screen_data={
                    'comment': 'One atomic source-tree ask.',
                    'escalation_evidence': evidence,
                },
                reason_code='org_local_operator_destination_unresolved',
            )

        result.refresh_from_db()
        self.assertEqual(result.status, 'blocked')
        self.assertNotEqual(result.status, 'waiting_external')
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(
            disposition.reason_code, 'org_local_operator_destination_unresolved'
        )
        self.assertIsNotNone(disposition.child_item)
        disposition.child_item.refresh_from_db()
        self.assertEqual(disposition.child_item.status, 'todo')
        self.assertTrue(disposition.child_item.assignment_group)

    def test_unresolved_operator_destination_does_not_park_schema_repair(self):
        item = self._item(title='Broken escalation envelope stays runnable')
        p1, p2, p3 = self._patches()
        with p1, p2, p3:
            result = escalation_routing.EscalationRoutingService._execute_forced_hold(
                item.pk,
                self.transition,
                user=self.user,
                screen_data={'comment': 'no typed evidence'},
                reason_code='org_local_operator_destination_unresolved',
            )

        result.refresh_from_db()
        self.assertEqual(result.status, 'open')

    def test_valid_human_escalation_parks_exact_org_group_owned_child(self):
        """PLS-837: an empty/ineligible exact team is ownership, not rejection."""
        item = self._item(title='Operator-held ask while exact roster has no seat')
        TeamRoster.objects.filter(
            organization_id=1,
            team_name='admin-ops',
        ).update(tiers=[])
        WorkflowService._invalidate_team_roster_caches()

        def empty_decision_team(group, **kwargs):
            if WorkflowService._canonical_team_name(group) == 'admin-ops':
                return None, None
            return self._resolver(group, **kwargs)

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=empty_decision_team,
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=self._apply,
        ), patch.object(
            escalation_routing,
            '_resolve_catalog',
            side_effect=lambda _item, ref: (ref, 7),
        ):
            result = WorkflowService.execute_transition(
                item,
                'blocked',
                user=self.user,
                screen_data={
                    'comment': 'One atomic statutory ask.',
                    'escalation_evidence': self._evidence(item),
                },
                transition_id=self.transition.id,
            )

        result.refresh_from_db()
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.assignment_group, 'admin-ops')
        self.assertIsNone(result.assignee_id)
        self.assertFalse(result.assignee_email)
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        blocker = disposition.child_item
        blocker.refresh_from_db()
        self.assertEqual(blocker.status, 'todo')
        self.assertEqual(blocker.assignment_group, 'admin-ops')
        self.assertIsNone(blocker.assignee_id)
        self.assertFalse(blocker.assignee_email)
        self.assertTrue(blocker.metadata['escalation_group_owned_wait'])
        self.assertEqual(blocker.metadata['escalation_group_owned_org_id'], 1)
        self.assertEqual(
            disposition.normalized_evidence['dependency']['holder_ref'],
            'gst-portal',
        )
        self.assertEqual(
            ItemLink.objects.filter(
                source_item=blocker,
                target_item=result,
                link_type='blocks',
            ).count(),
            1,
        )

    def test_valid_human_escalation_parks_when_roster_is_temporarily_ineligible(self):
        item = self._item(title='Operator-held ask while exact seat is unavailable')

        def unavailable_decision_team(group, **kwargs):
            if WorkflowService._canonical_team_name(group) == 'admin-ops':
                return None, None
            return self._resolver(group, **kwargs)

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=unavailable_decision_team,
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=self._apply,
        ):
            result = WorkflowService.execute_transition(
                item,
                'blocked',
                user=self.user,
                screen_data={
                    'comment': 'One atomic statutory ask.',
                    'escalation_evidence': self._evidence(item),
                },
                transition_id=self.transition.id,
            )

        disposition = EscalationRoutingDisposition.objects.get(item=result)
        blocker = disposition.child_item
        blocker.refresh_from_db()
        self.assertEqual(blocker.status, 'todo')
        self.assertIsNone(blocker.assignee_id)
        self.assertEqual(blocker.assignment_group, 'admin-ops')

        # Roster repair is an event, not a periodic scan: the exact roster
        # write enqueues the bounded org/team reconciler.
        roster = TeamRoster.objects.get(
            organization_id=1,
            team_name='admin-ops',
        )
        with patch(
            'tasks.tasks.reconcile_group_owned_external_waits.apply_async',
        ) as enqueue, self.captureOnCommitCallbacks(execute=True):
            roster.tiers = [[self.other.email]]
            roster.save(update_fields=['tiers', 'updated_at'])
        enqueue.assert_called_once_with(kwargs={
            'organization_id': 1,
            'team_name': 'admin-ops',
        })

        from tasks.services.external_wait import reconcile_group_owned_human_waits
        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.other.id, self.other.email),
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=self._apply,
        ):
            recovery = reconcile_group_owned_human_waits(
                organization_id=1,
                team_name='admin-ops',
            )

        self.assertEqual(recovery['assigned'], 1)
        blocker.refresh_from_db()
        result.refresh_from_db()
        self.assertEqual(blocker.status, 'todo')
        self.assertEqual(blocker.assignee_id, self.other.id)
        self.assertEqual(blocker.assignee_email, self.other.email)
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(
            ItemLink.objects.filter(
                source_item_id=blocker.pk,
                target_item_id=result.pk,
                link_type='blocks',
            ).count(),
            1,
        )

    def test_human_wait_revalidation_recovers_owner_after_empty_election(self):
        """Admission failure -> due retry -> recovery, without roster/agent events."""
        from tasks.models import ExternalWaitAuthority
        from tasks.tasks import revalidate_human_external_waits

        TeamRoster.objects.filter(
            organization_id=1, team_name='admin-ops',
        ).update(tiers=[[self.other.email]])
        WorkflowService._invalidate_team_roster_caches()
        parent = self._item(title='Human identity temporarily unavailable')
        other_parent = self._item(title='Another human wait is not due')
        with patch.object(
            WorkflowService, '_resolve_team_assignee', return_value=(None, None),
        ), patch.object(
            WorkflowService, '_apply_resolved_assignee', side_effect=self._apply,
        ):
            for item in (parent, other_parent):
                WorkflowService.execute_transition(
                    item, 'blocked', user=self.user,
                    screen_data={
                        'comment': 'A real human decision is required.',
                        'escalation_evidence': self._evidence(item, subject=item.title),
                    },
                    transition_id=self.transition.id,
                )
        carrier = EscalationRoutingDisposition.objects.get(item=parent).child_item
        other = EscalationRoutingDisposition.objects.get(item=other_parent).child_item
        self.assertNotEqual(carrier.pk, other.pk)
        authority = ExternalWaitAuthority.objects.get(item=carrier)
        original_generation = authority.wait_generation
        due = timezone.now() - timedelta(seconds=1)

        # First scheduled revalidation still has no identity. It must retain
        # the wait and arm the next existing revalidation, not manufacture an ID.
        ExternalWaitAuthority.objects.filter(pk=authority.pk).update(revalidate_after=due)
        with patch.object(
            WorkflowService, '_resolve_team_assignee', return_value=(None, None),
        ):
            revalidate_human_external_waits.run()
        carrier.refresh_from_db()
        self.assertIsNone(carrier.assignee_id)
        self.assertTrue(carrier.metadata['escalation_group_owned_wait'])

        # The next real caller invocation recovers the same carrier when the
        # canonical resolver becomes available; no roster write or heartbeat.
        ExternalWaitAuthority.objects.filter(pk=authority.pk).update(revalidate_after=due)
        with patch.object(
            WorkflowService, '_resolve_team_assignee',
            return_value=(self.other.id, self.other.email),
        ) as resolver, patch.object(
            WorkflowService, '_apply_resolved_assignee', side_effect=self._apply,
        ):
            revalidate_human_external_waits.run()
            revalidate_human_external_waits.run()
        self.assertEqual(resolver.call_count, 1)
        carrier.refresh_from_db()
        other.refresh_from_db()
        parent.refresh_from_db()
        authority.refresh_from_db()
        self.assertEqual(carrier.assignee_id, self.other.id)
        self.assertEqual(carrier.assignee_email, self.other.email)
        self.assertNotIn('escalation_group_owned_wait', carrier.metadata)
        self.assertNotIn('escalation_group_owned_cause', carrier.metadata)
        self.assertEqual(carrier.status, 'todo')
        self.assertNotEqual(carrier.status, 'waiting_external')
        self.assertEqual(authority.state, ExternalWaitAuthority.STATE_RELEASED)
        self.assertEqual(authority.wait_generation, original_generation)
        self.assertIsNone(authority.pre_assignee_id)
        self.assertFalse(authority.pre_assignee_email)
        self.assertEqual(parent.status, 'blocked')
        self.assertIsNone(other.assignee_id)
        self.assertTrue(other.metadata['escalation_group_owned_wait'])
        self.assertEqual(ItemLink.objects.filter(
            source_item=carrier, target_item=parent, link_type='blocks',
        ).count(), 1)

    def test_recovered_member_event_accepts_canonical_other_team_winner_once(self):
        """PLS-837: A's recovery may validly elect lower-load exact-Team peer B."""
        item = self._item(title='Operator-held ask awaiting exact Team recovery')

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(None, None),
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=self._apply,
        ):
            result = WorkflowService.execute_transition(
                item,
                'blocked',
                user=self.user,
                screen_data={
                    'comment': 'One atomic statutory ask.',
                    'escalation_evidence': self._evidence(item),
                },
                transition_id=self.transition.id,
            )

        disposition = EscalationRoutingDisposition.objects.get(item=result)
        blocker = disposition.child_item
        blocker.refresh_from_db()
        blocker_id = blocker.pk
        wait_generation = blocker.external_wait_generation
        TeamRoster.objects.filter(
            organization_id=1,
            team_name='admin-ops',
        ).update(tiers=[[self.user.email, self.other.email]])
        WorkflowService._invalidate_team_roster_caches()

        from tasks.services.external_wait import reconcile_group_owned_human_waits
        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            return_value=(self.other.id, self.other.email),
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=self._apply,
        ):
            first = reconcile_group_owned_human_waits(
                organization_id=1,
                team_name='admin-ops',
                only_agent_email=self.user.email,
            )
            second = reconcile_group_owned_human_waits(
                organization_id=1,
                team_name='admin-ops',
                only_agent_email=self.user.email,
            )

        self.assertEqual(first['assigned'], 1)
        self.assertEqual(second['assigned'], 0)
        blocker.refresh_from_db()
        result.refresh_from_db()
        self.assertEqual(blocker.pk, blocker_id)
        self.assertEqual(blocker.status, 'todo')
        self.assertEqual(blocker.external_wait_generation, wait_generation)
        self.assertEqual(blocker.assignee_id, self.other.id)
        self.assertEqual(blocker.assignee_email, self.other.email)
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(
            ItemLink.objects.filter(
                source_item_id=blocker_id,
                target_item_id=result.pk,
                link_type='blocks',
            ).count(),
            1,
        )

    def test_missing_schema_holds_captured_owner_and_never_falls_to_devops(self):
        item = self._item()

        result = self._execute(item, {})

        result.refresh_from_db()
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.assignment_group, 'engineering')
        self.assertEqual(result.assignee_email, self.user.email)
        self.assertTrue(WorkflowService._has_active_assigned_blocker(result))
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(disposition.classification, 'mixed_or_unknown_escalation_hold')
        self.assertEqual(disposition.reason_code, 'missing_evidence_schema')
        self.assertIsNotNone(disposition.child_item_id)
        self.assertEqual(
            result._escalation_receipt_payload['child_item_key'],
            disposition.child_item.item_key,
        )
        self.assertIn(
            '[SCHEMA REPAIR: missing_evidence_schema]',
            disposition.child_item.title,
        )
        self.assertNotEqual(disposition.child_item.status, 'waiting_external')
        self.assertIn(
            '"evidence_class": "human_held_source_evidence"',
            disposition.child_item.description,
        )
        self.assertIn(
            '"source_refs": [{"kind": "task", "id":',
            disposition.child_item.description,
        )
        self.assertEqual(
            disposition.child_item.metadata['escalation_expected_generation'],
            1,
        )
        self.assertTrue(Comment.objects.filter(item=result, content__icontains='ANDON').exists())

    def test_malformed_dependency_blocker_contains_complete_retry_contract(self):
        item = self._item()
        evidence = self._evidence(item)
        evidence['dependency'] = {'source_refs': [item.item_key]}

        result = self._execute(item, evidence)

        disposition = EscalationRoutingDisposition.objects.get(item=result)
        child = disposition.child_item
        self.assertEqual(disposition.reason_code, 'dependency_malformed')
        self.assertIn(
            '[SCHEMA REPAIR: dependency_malformed]',
            child.title,
        )
        self.assertIn('"requested_kind": "artifact"', child.description)
        self.assertIn('"holder_type": "operator"', child.description)
        self.assertIn(
            '"source_refs": [{"kind": "task", "id":',
            child.description,
        )
        self.assertIn(
            '`operator_held_dependency` / `routed`',
            child.description,
        )

    def test_nondefault_org_operator_evidence_routes_to_exact_org_decision_team(self):
        digital_project = Project.objects.create(
            name='Digital escalation project', key='TED', org_id=2,
        )
        TeamRoster.objects.update_or_create(
            organization_id=2,
            team_name='admin-ops',
            defaults={
                'organization_slug': 'shizuha-digital',
                'tiers': [['shion@shizuha.com']],
                'is_active': True,
            },
        )
        item = Item.objects.create(
            mode='task', item_type='task.bug',
            title='Holder-controlled Digital LLP source evidence',
            project=digital_project,
            item_key=digital_project.get_next_item_key(),
            workflow_name=self.workflow.slug,
            status='open', workflow_status='Open',
            assignment_group=None,
            assignee_id=None,
            assignee_email=None,
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )

        result = self._execute(item, self._evidence(item))

        result.refresh_from_db()
        self.assertEqual(result.assignment_group, 'admin-ops')
        self.assertEqual(result.assignee_email, 'shion@shizuha.com')
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(disposition.classification, 'operator_held_dependency')
        self.assertEqual(disposition.final_state['assignment_group'], 'admin-ops')
        self.assertIsNotNone(disposition.child_item_id)
        self.assertEqual(
            disposition.child_item.assignment_group,
            'admin-ops',
        )

    def test_missing_exact_org_decision_team_fails_with_diagnostic_without_mutation(self):
        TeamRoster.objects.filter(
            organization_id=1,
            team_name='admin-ops',
        ).delete()
        TeamRoster.objects.update_or_create(
            organization_id=2,
            team_name='admin-ops',
            defaults={
                'organization_slug': 'foreign-org',
                'tiers': [['foreign-holder@example.com']],
                'is_active': True,
            },
        )
        WorkflowService._invalidate_team_roster_caches()
        item = self._item()

        pre = {
            'status': item.status,
            'assignment_group': item.assignment_group,
            'assignee_id': item.assignee_id,
            'assignee_email': item.assignee_email,
        }
        with self.assertRaises(WorkflowValidationError) as exc:
            self._execute(item, self._evidence(item))

        self.assertEqual(exc.exception.code, 'BLOCKER_TEAM_UNREGISTERED')
        self.assertIn('organization_id=1', str(exc.exception))
        self.assertIn('team=`admin-ops`', str(exc.exception))
        item.refresh_from_db()
        self.assertEqual(item.status, pre['status'])
        self.assertEqual(item.assignment_group, pre['assignment_group'])
        self.assertEqual(item.assignee_id, pre['assignee_id'])
        self.assertEqual(item.assignee_email, pre['assignee_email'])
        self.assertFalse(WorkflowService._active_blockers_for(item))
        self.assertFalse(EscalationRoutingDisposition.objects.filter(item=item).exists())
        self.assertFalse(EscalationRoutingReceipt.objects.filter(item=item).exists())

    def test_repeated_invalid_retry_refreshes_contract_then_valid_retry_rehomes(self):
        item = self._item()
        first = self._execute(item, {})
        first_disposition = EscalationRoutingDisposition.objects.get(item=first)
        blocker_id = first_disposition.child_item_id

        second_evidence = self._evidence(first, expected=1)
        second_evidence['dependency'] = {'source_refs': [first.item_key]}
        second = self._execute(first, second_evidence)

        second.refresh_from_db()
        blocker = Item.objects.get(pk=blocker_id)
        second_disposition = EscalationRoutingDisposition.objects.get(
            item=second,
            generation=2,
        )
        self.assertEqual(second_disposition.child_item_id, blocker_id)
        self.assertEqual(second_disposition.reason_code, 'dependency_malformed')
        self.assertEqual(
            blocker.metadata['escalation_expected_generation'],
            2,
        )
        self.assertTrue(
            blocker.title.startswith('[SCHEMA REPAIR: dependency_malformed] '),
        )
        self.assertNotIn('missing_evidence_schema', blocker.title)
        self.assertEqual(blocker.title.count('[SCHEMA REPAIR:'), 1)
        self.assertIn('`dependency_malformed`', blocker.description)
        self.assertNotIn('`missing_evidence_schema`', blocker.description)
        self.assertIn('"expected_generation": 2', blocker.description)
        self.assertNotIn('"expected_generation": 1', blocker.description)

        result = self._execute(second, self._evidence(second, expected=2))

        result.refresh_from_db()
        blocker = Item.objects.get(pk=blocker_id)
        dispositions = list(
            EscalationRoutingDisposition.objects.filter(item=result).order_by('generation')
        )
        self.assertEqual(result.assignment_group, 'admin-ops')
        self.assertEqual(result.assignee_email, 'shion@shizuha.com')
        self.assertEqual(blocker.assignment_group, 'admin-ops')
        self.assertEqual(blocker.assignee_email, 'shion@shizuha.com')
        self.assertEqual(dispositions[-1].child_item_id, blocker_id)
        self.assertEqual(len(dispositions), 3)
        self.assertEqual(
            Item.objects.filter(metadata__auto_blocker_for=item.item_key).count(),
            1,
        )
        self.assertFalse(blocker.title.startswith('[SCHEMA REPAIR:'))
        self.assertNotIn('escalation_schema_repair', blocker.metadata)
        self.assertNotIn(
            '## Escalation evidence repair required',
            blocker.description,
        )

    def test_reused_blocker_assignment_failure_rolls_back_the_retry(self):
        item = self._item()
        first = self._execute(item, {})
        first_disposition = EscalationRoutingDisposition.objects.get(item=first)
        blocker_id = first_disposition.child_item_id
        evidence = self._evidence(first, expected=1)

        with patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=self._resolver,
        ), patch.object(
            WorkflowService,
            '_apply_resolved_assignee',
            side_effect=WorkflowValidationError(
                'injected guarded blocker assignment failure',
                code='injected_blocker_assignment_failure',
            ),
        ), patch.object(
            escalation_routing,
            '_resolve_catalog',
            side_effect=lambda _item, ref: (ref, 7),
        ), self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                first,
                'blocked',
                user=self.user,
                screen_data={
                    'comment': 'One atomic statutory ask.',
                    'escalation_evidence': evidence,
                },
                transition_id=self.transition.id,
            )

        first.refresh_from_db()
        blocker = Item.objects.get(pk=blocker_id)
        self.assertEqual(first.assignment_group, 'engineering')
        self.assertEqual(blocker.assignment_group, 'engineering')
        self.assertEqual(
            EscalationRoutingDisposition.objects.filter(item=first).count(),
            1,
        )
        self.assertEqual(
            EscalationRoutingNotificationOutbox.objects.filter(item=first).count(),
            1,
        )

    def test_negated_capability_phrase_does_not_override_typed_operator_evidence(self):
        item = self._item(title='This is not an agent capability gap; portal access is operator-held')
        evidence = self._evidence(
            item,
            subject='Operator-held source evidence, not an agent capability gap',
        )

        result = self._execute(item, evidence)

        result.refresh_from_db()
        self.assertEqual(result.assignment_group, 'admin-ops')
        self.assertEqual(
            EscalationRoutingDisposition.objects.get(item=result).classification,
            'operator_held_dependency',
        )

    def test_pure_internal_capability_routes_catalog_owner(self):
        item = self._item(title='Internal deployment permission')
        evidence = self._evidence(
            item,
            klass='internal_capability_gap', holder='service_team',
            kind='technical_action', holder_ref='devops',
            subject='Restore internal deployment permission',
        )

        result = self._execute(item, evidence)

        result.refresh_from_db()
        self.assertEqual(result.status, 'open')
        self.assertEqual(result.assignment_group, 'devops')
        self.assertEqual(result.assignee_email, 'san@shizuha.com')
        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(disposition.classification, 'internal_capability_gap')
        self.assertEqual(disposition.catalog_generation, 7)

    def test_mixed_evidence_creates_one_child_and_replay_is_quiet(self):
        item = self._item()
        evidence = self._evidence(
            item,
            technical_action={
                'action': 'Verify the internal Books connector health.',
                'service_ref': 'devops',
                'independently_executable': True,
                'independence_evidence': 'Connector health does not require the GSTIN.',
                'relationship': 'relates_to',
                'acceptance_check': 'Connector health endpoint is green.',
                'source_refs': [{'kind': 'task', 'id': item.item_key}],
            },
        )

        first = self._execute(item, evidence)
        comment_count = Comment.objects.filter(item=item).count()
        child_ids = list(Item.objects.filter(parent_item=item).values_list('id', flat=True))
        second = self._execute(first, evidence)

        self.assertEqual(len(child_ids), 1)
        self.assertEqual(Item.objects.filter(parent_item=item).count(), 1)
        self.assertEqual(EscalationRoutingDisposition.objects.filter(item=item).count(), 1)
        self.assertEqual(EscalationRoutingReceipt.objects.filter(item=item).count(), 1)
        self.assertEqual(Comment.objects.filter(item=item).count(), comment_count)
        self.assertTrue(second._escalation_receipt_payload['replayed'])

    def test_same_actor_new_transport_id_replays_same_receipt(self):
        item = self._item()
        first_evidence = self._evidence(item)
        first = self._execute(item, first_evidence)
        original_receipt = first._escalation_receipt_payload['receipt_id']
        second_evidence = dict(first_evidence)
        second_evidence['request_id'] = str(uuid.uuid4())

        second = self._execute(first, second_evidence)

        self.assertEqual(second._escalation_receipt_payload['receipt_id'], original_receipt)
        self.assertTrue(second._escalation_receipt_payload['replayed'])
        self.assertEqual(EscalationRoutingRequest.objects.filter(item=item).count(), 2)

    def test_cross_actor_convergence_keeps_distinct_attribution(self):
        item = self._item()
        first_evidence = self._evidence(item)
        first = self._execute(item, first_evidence)
        second_evidence = dict(first_evidence)
        second_evidence['request_id'] = str(uuid.uuid4())

        second = self._execute(first, second_evidence, user=self.other)

        self.assertEqual(EscalationRoutingDisposition.objects.filter(item=item).count(), 1)
        self.assertEqual(EscalationRoutingReceipt.objects.filter(item=item).count(), 2)
        subjects = set(EscalationRoutingReceipt.objects.filter(item=item).values_list('actor_subject_id', flat=True))
        self.assertEqual(len(subjects), 2)
        self.assertTrue(second._escalation_receipt_payload['replayed_disposition'])

    def test_secret_material_is_not_persisted_in_hold_evidence(self):
        item = self._item()
        evidence = self._evidence(
            item,
            subject='Use Authorization: Bearer aaa.bbb.ccc to inspect GST status',
        )

        result = self._execute(item, evidence)

        disposition = EscalationRoutingDisposition.objects.get(item=result)
        self.assertEqual(disposition.reason_code, 'secret_material_forbidden')
        persisted = json.dumps(disposition.normalized_evidence)
        self.assertNotIn('aaa.bbb.ccc', persisted)

    def test_request_id_reuse_with_different_semantics_is_conflict(self):
        item = self._item()
        request_id = str(uuid.uuid4())
        first_evidence = self._evidence(item, request_id=request_id)
        first = self._execute(item, first_evidence)
        changed = self._evidence(
            item, request_id=request_id, expected=1,
            subject='Different statutory subject',
        )

        with self.assertRaises(WorkflowValidationError) as raised:
            self._execute(first, changed)

        self.assertEqual(raised.exception.code, 'idempotency_key_reused')
        self.assertEqual(EscalationRoutingDisposition.objects.filter(item=item).count(), 1)

    def test_pls1195_fresh_uuid_after_prior_row_is_not_idempotency_conflict(self):
        """PLS-1195: a new request_id must not 409 against the item's prior escalation row."""
        item = self._item()
        first = self._execute(item, self._evidence(item, expected=0))
        retry = self._evidence(first, expected=1, subject='Post-repair atomic operator ask')

        second = self._execute(first, retry)

        self.assertNotEqual(
            second._escalation_receipt_payload['receipt_id'],
            first._escalation_receipt_payload['receipt_id'],
        )
        self.assertGreaterEqual(
            EscalationRoutingRequest.objects.filter(item=item).count(), 2,
        )

    def test_genuinely_new_stale_generation_is_conflict_without_mutation(self):
        item = self._item()
        first = self._execute(item, self._evidence(item))
        stale = self._evidence(
            item, expected=0, subject='A distinct new statutory subject',
        )

        with self.assertRaises(WorkflowValidationError) as raised:
            self._execute(first, stale)

        self.assertEqual(raised.exception.code, 'stale_escalation_generation')
        self.assertEqual(EscalationRoutingDisposition.objects.filter(item=item).count(), 1)

    def test_unexpected_plan_failure_rolls_back_to_one_owned_hold(self):
        item = self._item()
        evidence = self._evidence(
            item,
            technical_action={
                'action': 'Verify connector health.',
                'service_ref': 'devops',
                'independently_executable': True,
                'independence_evidence': 'The check is independent.',
                'relationship': 'relates_to',
                'acceptance_check': 'Health is green.',
                'source_refs': [{'kind': 'task', 'id': item.item_key}],
            },
        )
        p1, p2, p3 = self._patches()
        with p1, p2, p3, patch.object(
            escalation_routing, '_create_technical_child',
            side_effect=RuntimeError('injected child write failure'),
        ):
            result = WorkflowService.execute_transition(
                item, 'blocked', user=self.user,
                screen_data={'comment': 'Atomic ask.', 'escalation_evidence': evidence},
                transition_id=self.transition.id,
            )

        result.refresh_from_db()
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.assignment_group, 'engineering')
        self.assertEqual(Item.objects.filter(parent_item=item).count(), 0)
        self.assertTrue(WorkflowService._has_active_assigned_blocker(result))
        disposition = EscalationRoutingDisposition.objects.get(item=item)
        self.assertEqual(disposition.classification, 'mixed_or_unknown_escalation_hold')
        self.assertEqual(disposition.reason_code, 'unexpected_error')

class WorkflowStaleTimerTests(TestCase):
    """PLS-106: stale escalation policy is workflow config, not task-engine code."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Timer Project',
            key='TMR',
            org_id=1,
        )
        self.open_status, _ = Status.objects.get_or_create(
            slug='open',
            defaults={'name': 'Open', 'category': 'todo'},
        )
        self.blocked_status, _ = Status.objects.get_or_create(
            slug='blocked',
            defaults={'name': 'Blocked', 'category': 'in_progress'},
        )
        self.workflow = Workflow.objects.create(
            name='Timer Workflow',
            slug='timer-workflow',
            initial_status=self.open_status,
            is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow,
            status=self.open_status,
            position=0,
        )
        WorkflowStatus.objects.create(
            workflow=self.workflow,
            status=self.blocked_status,
            position=1,
        )
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Timer configured escalation',
            from_status='__timer__',
            to_status='__timer__',
            post_functions=[
                {
                    'type': 'stale_status_timer',
                    'assignment_groups': ['timer-team'],
                    'after_hours': 1,
                    'age_field': 'created_at',
                    'metadata_key': 'timer_escalation_marked_at',
                    'match_any': [
                        {
                            'statuses': ['blocked'],
                            'title_markers_any': ['needs timer'],
                            'title_markers_not_any': ['ignore timer'],
                        },
                    ],
                },
                {
                    'type': 'add_comment',
                    'body': (
                        'Timer fired for {{item_key}} from {{from_status}} '
                        'after {{timer_age_hours}}h (threshold '
                        '{{timer_threshold_hours}}h); '
                        '{{unknown_timer_value}}'
                    ),
                },
            ],
        )

    def _item(self, title='needs timer escalation', metadata=None):
        item = create_blocked_item(
            project=self.project,
            item_key='TMR-1',
            mode='task',
            title=title,
            status='blocked',
            workflow_status='blocked',
            workflow_name='timer-workflow',
            assignment_group='timer-team',
            created_by_email='agent@example.com',
            created_by_id=1,
            metadata=metadata or {},
        )
        satisfy_blocked_invariant(item)
        old = timezone.now() - timedelta(hours=3)
        Item.objects.filter(pk=item.pk).update(created_at=old, updated_at=old)
        item.refresh_from_db()
        return item

    def test_stale_timer_executes_configured_transition_post_functions(self):
        item = self._item()

        marked = _run_workflow_stale_timers(now=timezone.now())

        item.refresh_from_db()
        self.assertEqual(marked, ['TMR-1'])
        self.assertIn('timer_escalation_marked_at', item.metadata)
        self.assertIn(str(self.transition.id), item.metadata['stale_status_timers'])
        comment = Comment.objects.get(
            item=item,
            author_email='system@pulse',
            content__startswith='Timer fired for TMR-1',
        )
        self.assertEqual(
            comment.content,
            'Timer fired for TMR-1 from blocked after 3h '
            '(threshold 1h); {{unknown_timer_value}}',
        )

    def test_stale_timer_uses_configured_matchers_and_is_idempotent(self):
        item = self._item(title='ignore timer even though needs timer appears')

        self.assertEqual(_run_workflow_stale_timers(now=timezone.now()), [])

        item.title = 'needs timer escalation'
        item.save(update_fields=['title'])
        self.assertEqual(_run_workflow_stale_timers(now=timezone.now()), ['TMR-1'])
        self.assertEqual(_run_workflow_stale_timers(now=timezone.now()), [])
        self.assertEqual(
            Comment.objects.filter(item=item, content__contains='Timer fired for TMR-1').count(),
            1,
        )


@override_settings(PLS482_PUBLICATION_BINDING_ENABLED=True)
class PublicationHandoffSagaTests(TestCase):
    """PLS-482 production-order prepare -> Wiki CAS -> finalize -> delivery.

    The P2 #2 activation gate is enabled for these tests; the gate-off
    fail-closed behavior has its own regressions below."""

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='publisher', email='publisher@shizuha.com', password='x'
        )
        self.user.organization_memberships = {1: 'member'}
        self.project = Project.objects.create(name='Publication', key='PUB', org_id=1)
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user.id,
            user_email=self.user.email,
            role='member',
            is_active=True,
            grant_source='independent',
        )
        self.open_status = Status.objects.create(name='Open Pub', slug='pub_open', category='todo')
        self.review_status = Status.objects.create(name='Review Pub', slug='pub_review', category='in_progress')
        self.workflow = Workflow.objects.create(
            name='Publication Workflow',
            slug='publication-workflow',
            initial_status=self.open_status,
        )
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.open_status, position=0)
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.review_status, position=1)
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Publish for Review',
            from_status='pub_open',
            to_status='pub_review',
            post_functions=[{'type': 'publication_handoff'}],
        )
        with patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            self.item = Item.objects.create(
                project=self.project,
                item_key='PUB-1',
                mode='task',
                item_type='task.generic',
                title='Immutable handoff',
                status='pub_open',
                workflow_status='pub_open',
                workflow_name=self.workflow.slug,
                assignee_id=self.user.id,
                assignee_email=self.user.email,
                assignment_group='',
                created_by_id=self.user.id,
                created_by_email=self.user.email,
                tags='no-pr',
                metadata={'publication_review_round_id': 'round-7'},
            )
        # Per Sora/aoi's binding P1 #1 ruling (HLD v23 §17): PLS-482 holds NO
        # local artifact record and the caller supplies no artifact authority.
        # This transition has no `artifact_bearing` flag => server-classified
        # Wiki-only => artifact_authority_ref = null.
        self.pages = [{
            'page_id': '11111111-1111-4111-8111-111111111111',
            'version': 5,
            'anchors': [{'anchor': 'contract', 'section_hash': 'e' * 64}],
        }]
        self.screen = {
            'publication_binding': {
                'operation_key': 'pub-op-7',
                'review_round_id': 'round-7',
                'publication_result': 'accepted',
                'pages': self.pages,
            },
        }
        self.wiki_receipt = self._receipt(self.pages)

    def _receipt(self, pages, *, organization_id=1, publication_result='accepted',
                 snapshot_id='22222222-2222-4222-8222-222222222222'):
        """A well-formed service-principal CAS receipt (§17.2.4). Pulse recomputes
        the domain-separated snapshot digest, so the receipt must carry the
        matching digest, organization, result, and ordered pages — an ID plus 64
        hex characters is not proof (P1 #5)."""
        return {
            'publication_snapshot_id': snapshot_id,
            'organization_id': organization_id,
            'publication_result': publication_result,
            'pages': pages,
            'snapshot_digest': _digest(
                'io.shizuha.wiki-publication-snapshot/v1',
                {
                    'organization_id': organization_id,
                    'publication_result': publication_result,
                    'pages': pages,
                },
            ),
            'replay': False,
        }

    def _new_item(self, item_key, review_round_id):
        with patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            return Item.objects.create(
                project=self.project,
                item_key=item_key,
                mode='task',
                item_type='task.generic',
                title=f'Immutable handoff {item_key}',
                status='pub_open',
                workflow_status='pub_open',
                workflow_name=self.workflow.slug,
                assignee_id=self.user.id,
                assignee_email=self.user.email,
                assignment_group='',
                created_by_id=self.user.id,
                created_by_email=self.user.email,
                tags='no-pr',
                metadata={'publication_review_round_id': review_round_id},
            )

    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_production_order_commits_one_binding_outbox_and_exact_replay(self, wiki_snapshot):
        wiki_snapshot.return_value = self.wiki_receipt
        with self.captureOnCommitCallbacks(execute=False):
            result = WorkflowService.execute_transition(
                self.item,
                'pub_review',
                user=self.user,
                screen_data=self.screen,
                transition_id=str(self.transition.id),
            )
        self.assertEqual(result.status, 'pub_review')
        self.assertEqual(PublicationAttempt.objects.get().state, 'published')
        self.assertEqual(PublicationBinding.objects.count(), 1)
        self.assertEqual(PublicationOutbox.objects.count(), 1)
        self.assertEqual(Notification.objects.count(), 0)

        # Exact top-level replay after the item already moved returns the same
        # committed outcome and cannot create a second snapshot/binding/outbox.
        replay_item = WorkflowService.execute_transition(
            Item.objects.get(pk=self.item.pk),
            'pub_review',
            user=self.user,
            screen_data=self.screen,
            transition_id=str(self.transition.id),
        )
        self.assertEqual(replay_item.status, 'pub_review')
        self.assertEqual(PublicationBinding.objects.count(), 1)
        self.assertEqual(PublicationOutbox.objects.count(), 1)
        wiki_snapshot.assert_called_once()

        # The immutable downstream tuple preserves the exact closed-receipt
        # digest across the top-level replay, carrier metadata, and outbox.
        expected_snapshot_digest = self.wiki_receipt['snapshot_digest']
        binding = PublicationBinding.objects.get()
        outbox = PublicationOutbox.objects.get()
        replay_item.refresh_from_db()
        self.assertEqual(
            binding.binding_tuple['snapshot_digest'], expected_snapshot_digest
        )
        self.assertEqual(
            replay_item.metadata['publication_binding']['snapshot_digest'],
            expected_snapshot_digest,
        )
        self.assertEqual(outbox.payload['snapshot_digest'], expected_snapshot_digest)

        first = deliver_publication_outbox.run(str(outbox.id))
        second = deliver_publication_outbox.run(str(outbox.id))
        outbox.refresh_from_db()
        self.assertEqual(first['status'], 'delivered')
        self.assertEqual(second['status'], 'delivered')
        self.assertEqual(Notification.objects.count(), 1)
        self.assertEqual(outbox.binding.attempt.state, 'delivered')

    @patch('tasks.services.publication_handoff.requests.post')
    def test_malformed_snapshot_uuid_is_typed_terminal_receipt_hold(self, wiki_post):
        """A truthy but malformed snapshot ID is rejected at the real Wiki
        boundary before the UUID-backed attempt field is written. The remote CAS
        receipt is terminally held instead of entering an untyped retry loop."""
        malformed = dict(self.wiki_receipt)
        malformed['publication_snapshot_id'] = 'not-a-uuid'
        wiki_post.return_value.status_code = 201
        wiki_post.return_value.json.return_value = malformed

        with override_settings(SHIZUHA_WIKI_SERVICE_USER_ID=901):
            with self.assertRaises(WorkflowValidationError) as caught:
                WorkflowService.execute_transition(
                    self.item,
                    'pub_review',
                    user=self.user,
                    screen_data=self.screen,
                    transition_id=str(self.transition.id),
                )

        self.assertEqual(caught.exception.code, 'CARRIER_CONFLICT_HOLD')
        attempt = PublicationAttempt.objects.get()
        self.assertEqual(attempt.state, 'carrier_conflict_hold')
        self.assertEqual(attempt.failure_code, 'wiki_invalid_response')
        self.assertIsNone(attempt.wiki_snapshot_id)
        self.assertEqual(PublicationBinding.objects.count(), 0)
        self.assertEqual(PublicationOutbox.objects.count(), 0)
        wiki_post.assert_called_once()

    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_crash_after_wiki_commit_resumes_without_second_snapshot(self, wiki_snapshot):
        """A crash between systems resumes from the durable Wiki receipt."""
        wiki_snapshot.return_value = self.wiki_receipt
        with patch(
            'tasks.services.publication_handoff.PublicationBinding.objects.get_or_create',
            side_effect=RuntimeError('simulated process death before Pulse commit'),
        ), self.assertRaisesRegex(RuntimeError, 'simulated process death'):
            WorkflowService.execute_transition(
                self.item,
                'pub_review',
                user=self.user,
                screen_data=self.screen,
                transition_id=str(self.transition.id),
            )

        attempt = PublicationAttempt.objects.get()
        self.assertEqual(attempt.state, 'snapshot_committed')
        self.assertEqual(PublicationBinding.objects.count(), 0)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'pub_open')

        with self.captureOnCommitCallbacks(execute=False):
            resumed = WorkflowService.execute_transition(
                self.item,
                'pub_review',
                user=self.user,
                screen_data=self.screen,
                transition_id=str(self.transition.id),
            )
        self.assertEqual(resumed.status, 'pub_review')
        self.assertEqual(PublicationBinding.objects.count(), 1)
        self.assertEqual(PublicationOutbox.objects.count(), 1)
        wiki_snapshot.assert_called_once()

    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_wiki_only_publication_binds_null_artifact_authority(self, wiki_snapshot):
        """§17.4 #7 — a Wiki-only publication succeeds with an explicit null
        artifact authority and mints no artifact metadata (the forbidden store)."""
        wiki_snapshot.return_value = self.wiki_receipt
        with self.captureOnCommitCallbacks(execute=False):
            WorkflowService.execute_transition(
                self.item, 'pub_review', user=self.user, screen_data=self.screen,
                transition_id=str(self.transition.id),
            )
        attempt = PublicationAttempt.objects.get()
        self.assertEqual(attempt.state, 'published')
        self.assertEqual(attempt.artifact_policy, 'not_required')
        self.assertIsNone(attempt.artifact_authority_ref)
        binding = PublicationBinding.objects.get()
        self.assertIn('artifact_authority_ref', binding.binding_tuple)
        self.assertIsNone(binding.binding_tuple['artifact_authority_ref'])

    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_artifact_bearing_without_provider_fails_closed(self, wiki_snapshot):
        """§17.4 #8 (disputed-finding regression, per aoi) — an artifact-bearing
        handoff fails closed with artifact_authority_unavailable_hold BEFORE Wiki
        CAS, because no canonical PLAT-4595 provider is implemented and PLS-482
        never mints a substitute."""
        bearing = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Publish Artifact for Review',
            from_status='pub_open',
            to_status='pub_review',
            post_functions=[{'type': 'publication_handoff', 'artifact_bearing': True}],
        )
        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService.execute_transition(
                self.item, 'pub_review', user=self.user, screen_data=self.screen,
                transition_id=str(bearing.id),
            )
        self.assertEqual(caught.exception.code, 'ARTIFACT_AUTHORITY_UNAVAILABLE_HOLD')
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'pub_open')
        self.assertEqual(
            PublicationAttempt.objects.get().state, 'artifact_authority_unavailable_hold'
        )
        self.assertEqual(PublicationBinding.objects.count(), 0)
        wiki_snapshot.assert_not_called()

    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_stale_hold_retires_review_round_and_blocks_reuse(self, wiki_snapshot):
        """§17.4 #5 — a stale_publication_hold consumes/retires its review round;
        no later attempt (even under a fresh operation key) can reuse it."""
        from tasks.services.publication_handoff import PublicationWikiError
        wiki_snapshot.side_effect = PublicationWikiError(
            'stale_publication_hold', 'a Wiki edit won before the snapshot CAS.',
            {'current_pages': []},
        )
        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService.execute_transition(
                self.item, 'pub_review', user=self.user, screen_data=self.screen,
                transition_id=str(self.transition.id),
            )
        self.assertEqual(caught.exception.code, 'STALE_PUBLICATION_HOLD')
        self.assertTrue(PublicationConsumedRound.objects.filter(
            item=self.item, review_round_id='round-7'
        ).exists())
        # A fresh operation key that reuses the retired round is rejected up front.
        retry_screen = {'publication_binding': dict(
            self.screen['publication_binding'], operation_key='pub-op-7-retry'
        )}
        with self.assertRaises(WorkflowValidationError) as retry:
            WorkflowService.execute_transition(
                Item.objects.get(pk=self.item.pk), 'pub_review', user=self.user,
                screen_data=retry_screen, transition_id=str(self.transition.id),
            )
        self.assertEqual(retry.exception.code, 'STALE_PUBLICATION_HOLD')
        wiki_snapshot.assert_called_once()

    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_plat4571_obsolete_v4_v23_carrier_cannot_publish_v5_v24_heads(
        self, wiki_snapshot,
    ):
        """§17.4 #1 / PLAT-4571: a carrier still naming PLAT-301 v4 and
        OPS v23 cannot publish after the canonical heads become v5 and v24."""
        from tasks.services.publication_handoff import PublicationWikiError

        carrier_pages = [
            {
                'page_id': '30130130-1301-4301-8301-301301301301',
                'version': 4,
                'anchors': [{'anchor': 'decision', 'section_hash': 'a' * 64}],
            },
            {
                'page_id': '02402402-4024-4024-8024-024024024024',
                'version': 23,
                'anchors': [{'anchor': 'runbook', 'section_hash': 'b' * 64}],
            },
        ]
        current_pages = [
            {**carrier_pages[0], 'version': 5},
            {**carrier_pages[1], 'version': 24},
        ]
        metadata = dict(self.item.metadata)
        metadata['publication_pages'] = carrier_pages
        self.item.metadata = metadata
        self.item.save(update_fields=['metadata', 'updated_at'])
        screen = {'publication_binding': {
            **self.screen['publication_binding'],
            'pages': carrier_pages,
        }}
        wiki_snapshot.side_effect = PublicationWikiError(
            'stale_publication_hold',
            'canonical Wiki heads advanced beyond the carrier baseline.',
            {'current_pages': current_pages},
        )

        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService.execute_transition(
                self.item, 'pub_review', user=self.user, screen_data=screen,
                transition_id=str(self.transition.id),
            )

        self.assertEqual(caught.exception.code, 'STALE_PUBLICATION_HOLD')
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'pub_open')
        self.assertEqual(self.item.metadata['publication_pages'], carrier_pages)
        attempt = PublicationAttempt.objects.get()
        self.assertEqual(attempt.expected_pages, carrier_pages)
        self.assertEqual(attempt.failure_evidence['current_pages'], current_pages)
        self.assertEqual(PublicationBinding.objects.count(), 0)
        self.assertEqual(PublicationOutbox.objects.count(), 0)

    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_successive_review_time_wiki_head_changes_retire_each_round(
        self, wiki_snapshot,
    ):
        """§17.4 #2 / PLAT-4159 / HIVE-277: successive canonical edits
        consume successive review rounds; neither stale tuple is published."""
        from tasks.services.publication_handoff import PublicationWikiError

        def stale(current_version):
            return PublicationWikiError(
                'stale_publication_hold',
                f'Wiki advanced to v{current_version} during review.',
                {'current_pages': [{**self.pages[0], 'version': current_version}]},
            )

        wiki_snapshot.side_effect = [stale(6), stale(7)]
        for round_id, operation_key, expected_version in (
            ('round-7', 'pub-op-7', 5),
            ('round-8', 'pub-op-8', 6),
        ):
            metadata = dict(self.item.metadata)
            metadata['publication_review_round_id'] = round_id
            self.item.metadata = metadata
            self.item.save(update_fields=['metadata', 'updated_at'])
            pages = [{**self.pages[0], 'version': expected_version}]
            screen = {'publication_binding': {
                **self.screen['publication_binding'],
                'operation_key': operation_key,
                'review_round_id': round_id,
                'pages': pages,
            }}
            with self.assertRaises(WorkflowValidationError) as caught:
                WorkflowService.execute_transition(
                    self.item, 'pub_review', user=self.user, screen_data=screen,
                    transition_id=str(self.transition.id),
                )
            self.assertEqual(caught.exception.code, 'STALE_PUBLICATION_HOLD')

        self.assertEqual(
            set(PublicationConsumedRound.objects.values_list('review_round_id', flat=True)),
            {'round-7', 'round-8'},
        )
        self.assertEqual(
            set(PublicationAttempt.objects.values_list('state', flat=True)),
            {'stale_publication_hold'},
        )
        self.assertEqual(PublicationAttempt.objects.count(), 2)
        self.assertEqual(PublicationBinding.objects.count(), 0)
        self.assertEqual(PublicationOutbox.objects.count(), 0)

    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_rejects_each_mismatched_closed_wiki_receipt_field(self, wiki_snapshot):
        """§17.4 #6: organization, result, ordered pages, and digest are all
        authority fences; a mismatch in any one produces zero visible handoff."""
        cases = {}
        receipt = self._receipt(self.pages)
        cases['organization'] = {**receipt, 'organization_id': 2}
        cases['result'] = {**receipt, 'publication_result': 'rejected'}
        cases['pages'] = {
            **receipt,
            'pages': [{**self.pages[0], 'version': 6}],
        }
        cases['digest'] = {**receipt, 'snapshot_digest': 'f' * 64}

        for index, (field, mismatched_receipt) in enumerate(cases.items(), start=1):
            with self.subTest(field=field):
                round_id = f'mismatch-round-{index}'
                item = self._new_item(f'PUB-MISMATCH-{index}', round_id)
                screen = {'publication_binding': {
                    **self.screen['publication_binding'],
                    'operation_key': f'pub-mismatch-{index}',
                    'review_round_id': round_id,
                }}
                wiki_snapshot.return_value = mismatched_receipt
                with self.assertRaises(WorkflowValidationError) as caught:
                    WorkflowService.execute_transition(
                        item, 'pub_review', user=self.user, screen_data=screen,
                        transition_id=str(self.transition.id),
                    )
                self.assertEqual(caught.exception.code, 'CARRIER_CONFLICT_HOLD')
                item.refresh_from_db()
                self.assertEqual(item.status, 'pub_open')
                attempt = PublicationAttempt.objects.get(item=item)
                self.assertEqual(attempt.state, 'carrier_conflict_hold')
                self.assertEqual(attempt.failure_code, 'wiki_receipt_mismatch')

        self.assertEqual(PublicationBinding.objects.count(), 0)
        self.assertEqual(PublicationOutbox.objects.count(), 0)

    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_carrier_drift_after_wiki_cas_is_terminal_hold_with_zero_visible_handoff(self, wiki_snapshot):
        def drift(**_kwargs):
            item = Item.objects.get(pk=self.item.pk)
            item.title = 'racing carrier edit'
            item.save(update_fields=['title', 'updated_at'])
            return self.wiki_receipt
        wiki_snapshot.side_effect = drift

        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService.execute_transition(
                self.item, 'pub_review', user=self.user, screen_data=self.screen,
                transition_id=str(self.transition.id),
            )

        self.assertEqual(caught.exception.code, 'CARRIER_CONFLICT_HOLD')
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'pub_open')
        attempt = PublicationAttempt.objects.get()
        self.assertEqual(attempt.state, 'carrier_conflict_hold')
        self.assertIsNotNone(attempt.wiki_snapshot_id)
        self.assertEqual(PublicationBinding.objects.count(), 0)
        self.assertEqual(PublicationOutbox.objects.count(), 0)

    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_gate_off_fails_closed_and_preserves_history(self, wiki_snapshot):
        """P2 #2 / §17.3: gate-off stops NEW attempts fail-closed (never a
        legacy unfenced emission) while every historical row — snapshot
        reference, binding, outbox — is preserved and still deliverable."""
        wiki_snapshot.return_value = self.wiki_receipt
        # Publish once while the gate is on (class-level override).
        with self.captureOnCommitCallbacks(execute=False):
            WorkflowService.execute_transition(
                self.item, 'pub_review', user=self.user, screen_data=self.screen,
                transition_id=str(self.transition.id),
            )
        self.assertEqual(PublicationBinding.objects.count(), 1)

        with override_settings(PLS482_PUBLICATION_BINDING_ENABLED=False):
            # A NEW attempt on a second carrier fails closed: the transition is
            # blocked (no legacy emission => status unchanged) with a typed code.
            with patch.object(
                WorkflowService, 'assignment_org_mismatch',
                return_value=(False, 'shizuha', 'shizuha'),
            ):
                second = Item.objects.create(
                    project=self.project,
                    item_key='PUB-2',
                    mode='task',
                    item_type='task.generic',
                    title='Gated handoff',
                    status='pub_open',
                    workflow_status='pub_open',
                    workflow_name=self.workflow.slug,
                    assignee_id=self.user.id,
                    assignee_email=self.user.email,
                    assignment_group='',
                    created_by_id=self.user.id,
                    created_by_email=self.user.email,
                    tags='no-pr',
                    metadata={'publication_review_round_id': 'round-8'},
                )
            gated_screen = {'publication_binding': dict(
                self.screen['publication_binding'],
                operation_key='pub-op-8', review_round_id='round-8',
            )}
            with self.assertRaises(WorkflowValidationError) as caught:
                WorkflowService.execute_transition(
                    second, 'pub_review', user=self.user, screen_data=gated_screen,
                    transition_id=str(self.transition.id),
                )
            self.assertEqual(caught.exception.code, 'PUBLICATION_GATE_OFF')
            second.refresh_from_db()
            self.assertEqual(second.status, 'pub_open')

            # History preserved: nothing was deleted or downgraded...
            self.assertEqual(PublicationAttempt.objects.count(), 1)
            self.assertEqual(PublicationBinding.objects.count(), 1)
            self.assertEqual(PublicationOutbox.objects.count(), 1)
            # ...and the already-committed outbox still delivers under gate-off.
            outbox = PublicationOutbox.objects.get()
            result = deliver_publication_outbox.run(str(outbox.id))
            self.assertEqual(result['status'], 'delivered')
            self.assertEqual(Notification.objects.count(), 1)

    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_gate_off_exact_replay_of_terminal_success_still_succeeds(self, wiki_snapshot):
        """Supplemental P1 #3: after gate-off rollback, an exact lost-response
        replay of an already-published attempt is replay SUCCESS — never
        PUBLICATION_GATE_OFF — with zero new rows or effects."""
        wiki_snapshot.return_value = self.wiki_receipt
        with self.captureOnCommitCallbacks(execute=False):
            WorkflowService.execute_transition(
                self.item, 'pub_review', user=self.user, screen_data=self.screen,
                transition_id=str(self.transition.id),
            )
        self.assertEqual(PublicationAttempt.objects.get().state, 'published')

        with override_settings(PLS482_PUBLICATION_BINDING_ENABLED=False):
            replay = WorkflowService.execute_transition(
                Item.objects.get(pk=self.item.pk), 'pub_review', user=self.user,
                screen_data=self.screen, transition_id=str(self.transition.id),
            )
        self.assertEqual(replay.status, 'pub_review')
        self.assertEqual(PublicationAttempt.objects.count(), 1)
        self.assertEqual(PublicationBinding.objects.count(), 1)
        self.assertEqual(PublicationOutbox.objects.count(), 1)
        wiki_snapshot.assert_called_once()

    @patch('tasks.services.publication_handoff._wiki_snapshot')
    def test_stale_hold_and_round_retirement_commit_atomically(self, wiki_snapshot):
        """Supplemental P1 #2: a failure between round retirement and the hold
        write leaves NO committed half-state — both or neither."""
        from tasks.services.publication_handoff import PublicationWikiError
        wiki_snapshot.side_effect = PublicationWikiError(
            'stale_publication_hold', 'a Wiki edit won before the snapshot CAS.',
            {'current_pages': []},
        )
        # Inject a crash on the attempt-state save INSIDE the atomic block: the
        # already-written consumed-round row must roll back with it.
        real_save = PublicationAttempt.save
        def exploding_save(self_, *args, **kwargs):
            if self_.state == 'stale_publication_hold':
                raise RuntimeError('simulated crash between retirement and hold')
            return real_save(self_, *args, **kwargs)
        with patch.object(PublicationAttempt, 'save', exploding_save), \
                self.assertRaisesRegex(RuntimeError, 'simulated crash'):
            WorkflowService.execute_transition(
                self.item, 'pub_review', user=self.user, screen_data=self.screen,
                transition_id=str(self.transition.id),
            )
        # No half-state: neither a consumed round nor a stale hold committed.
        self.assertFalse(PublicationConsumedRound.objects.filter(
            item=self.item, review_round_id='round-7'
        ).exists())
        self.assertNotEqual(
            PublicationAttempt.objects.get().state, 'stale_publication_hold'
        )
        # The retry (no injection) commits BOTH atomically.
        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService.execute_transition(
                Item.objects.get(pk=self.item.pk), 'pub_review', user=self.user,
                screen_data=self.screen, transition_id=str(self.transition.id),
            )
        self.assertEqual(caught.exception.code, 'STALE_PUBLICATION_HOLD')
        self.assertTrue(PublicationConsumedRound.objects.filter(
            item=self.item, review_round_id='round-7'
        ).exists())
        self.assertEqual(
            PublicationAttempt.objects.get().state, 'stale_publication_hold'
        )

    def test_wiki_token_requires_configured_service_principal(self):
        """Supplemental P1 #1: the REAL minted token carries the configured
        principal claim (decoded, not mocked), and minting fails closed when
        the principal is unset — before any network call."""
        import jwt as pyjwt
        from django.conf import settings as dj_settings
        from tasks.services.publication_handoff import (
            PublicationWikiError, _mint_wiki_token,
        )
        with override_settings(SHIZUHA_WIKI_SERVICE_USER_ID=0):
            with self.assertRaises(PublicationWikiError) as caught:
                _mint_wiki_token(1)
            self.assertEqual(caught.exception.code, 'wiki_auth_unavailable')
        with override_settings(SHIZUHA_WIKI_SERVICE_USER_ID=901):
            token = _mint_wiki_token(1)
            signing_key = dj_settings.SIMPLE_JWT.get('SIGNING_KEY') or dj_settings.SECRET_KEY
            claims = pyjwt.decode(
                token, signing_key, algorithms=['HS256'], audience='wiki',
            )
            # The exact claim Wiki's principal gate enforces (wiki#19 f91edc5):
            # request.user.id == SHIZUHA_WIKI_SERVICE_USER_ID, org-scoped.
            self.assertEqual(claims['user_id'], 901)
            self.assertEqual(claims['username'], 'pulse-publication')
            self.assertEqual(claims['organization_memberships'], {'1': 'member'})

    @patch('tasks.services.notifications.notification_service.notify_user')
    def test_outbox_failure_dms_owner_at_threshold(self, notify_user):
        """Reika P2 (PLAT-1254 owner-DM leg): after the retry threshold the
        failure reaches the item owner through notification_service.notify_user
        (all active channels incl. Connect DM/push), exactly once."""
        from celery.exceptions import Retry as CeleryRetry
        with patch('tasks.services.publication_handoff._wiki_snapshot',
                   return_value=self.wiki_receipt), \
                self.captureOnCommitCallbacks(execute=False):
            WorkflowService.execute_transition(
                self.item, 'pub_review', user=self.user, screen_data=self.screen,
                transition_id=str(self.transition.id),
            )
        outbox = PublicationOutbox.objects.get()
        # Force three consecutive delivery failures at the Notification write.
        with patch('tasks.models.Notification.objects') as broken:
            broken.create.side_effect = RuntimeError('injected delivery failure')
            for attempt_no in (1, 2, 3):
                with self.assertRaises((CeleryRetry, RuntimeError)):
                    deliver_publication_outbox.run(str(outbox.id))
        outbox.refresh_from_db()
        self.assertEqual(outbox.attempts, 3)
        notify_user.assert_called_once()
        kwargs = notify_user.call_args.kwargs
        self.assertEqual(kwargs['user_id'], self.item.assignee_id)
        self.assertEqual(kwargs['event_type'], 'alert')
        self.assertIn('publication delivery FAILING', kwargs['title'])


@override_settings(PLS482_PUBLICATION_BINDING_ENABLED=True)
class PublicationHandoffConcurrencyTests(TransactionTestCase):
    """§17.4 production-order races require real commits and row locks."""

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='publication-racer',
            email='publication-racer@shizuha.com',
            password='x',
        )
        self.user.organization_memberships = {1: 'member'}
        self.project = Project.objects.create(name='Publication Race', key='PUBR', org_id=1)
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user.id,
            user_email=self.user.email,
            role='member',
            is_active=True,
            grant_source='independent',
        )
        open_status = Status.objects.create(
            name='Open Publication Race', slug='pub_race_open', category='todo'
        )
        review_status = Status.objects.create(
            name='Review Publication Race', slug='pub_race_review', category='in_progress'
        )
        self.workflow = Workflow.objects.create(
            name='Publication Race Workflow',
            slug='publication-race-workflow',
            initial_status=open_status,
        )
        WorkflowStatus.objects.create(workflow=self.workflow, status=open_status, position=0)
        WorkflowStatus.objects.create(workflow=self.workflow, status=review_status, position=1)
        self.transition = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Publish Race for Review',
            from_status='pub_race_open',
            to_status='pub_race_review',
            post_functions=[{'type': 'publication_handoff'}],
        )
        self.pages = [{
            'page_id': '33333333-3333-4333-8333-333333333333',
            'version': 5,
            'anchors': [{'anchor': 'contract', 'section_hash': 'c' * 64}],
        }]

    def _item(self, item_key, review_round_id):
        with patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            return Item.objects.create(
                project=self.project,
                item_key=item_key,
                mode='task',
                item_type='task.generic',
                title=f'Concurrent publication {item_key}',
                status='pub_race_open',
                workflow_status='pub_race_open',
                workflow_name=self.workflow.slug,
                assignee_id=self.user.id,
                assignee_email=self.user.email,
                assignment_group='',
                created_by_id=self.user.id,
                created_by_email=self.user.email,
                tags='no-pr',
                metadata={'publication_review_round_id': review_round_id},
            )

    def _screen(self, operation_key, review_round_id):
        return {'publication_binding': {
            'operation_key': operation_key,
            'review_round_id': review_round_id,
            'publication_result': 'accepted',
            'pages': self.pages,
        }}

    def _receipt(self):
        return {
            'publication_snapshot_id': '44444444-4444-4444-8444-444444444444',
            'organization_id': 1,
            'publication_result': 'accepted',
            'pages': self.pages,
            'snapshot_digest': _digest(
                'io.shizuha.wiki-publication-snapshot/v1',
                {
                    'organization_id': 1,
                    'publication_result': 'accepted',
                    'pages': self.pages,
                },
            ),
            'replay': False,
        }

    def _execute_in_worker(self, item_id, screen, results, errors):
        close_old_connections()
        try:
            item = Item.objects.get(pk=item_id)
            user = get_user_model().objects.get(pk=self.user.pk)
            user.organization_memberships = {1: 'member'}
            result = WorkflowService.execute_transition(
                item,
                'pub_race_review',
                user=user,
                screen_data=screen,
                transition_id=str(self.transition.id),
            )
            results.append((item_id, result.status))
        except Exception as exc:  # surfaced below with its concrete typed code
            errors.append(exc)
        finally:
            close_old_connections()

    @skipUnless(connection.vendor == 'postgresql', 'row-lock concurrency requires PostgreSQL')
    def test_concurrent_exact_replay_returns_published_success_after_snapshot_lock(self):
        """§17.4 #4: both top-level callers pass PREPARE and Wiki CAS; one
        publishes before the other crosses the snapshot-commit lock. Both get
        the one immutable success, never PUBLICATION_STATE_CONFLICT."""
        item = self._item('PUBR-1', 'race-round-1')
        screen = self._screen('race-operation-1', 'race-round-1')
        receipt = self._receipt()
        wiki_barrier = threading.Barrier(2)
        call_lock = threading.Lock()
        call_number = 0

        def concurrent_snapshot(**_kwargs):
            nonlocal call_number
            with call_lock:
                ordinal = call_number
                call_number += 1
            wiki_barrier.wait(timeout=10)
            if ordinal == 1:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    state = PublicationAttempt.objects.get(
                        operation_key='race-operation-1'
                    ).state
                    if state == 'published':
                        break
                    time.sleep(0.01)
                else:
                    self.fail('winning caller did not publish before replay resumed')
            return receipt

        results = []
        errors = []
        with patch(
            'tasks.services.publication_handoff._wiki_snapshot',
            side_effect=concurrent_snapshot,
        ), patch.object(deliver_publication_outbox, 'delay', return_value=None):
            threads = [
                threading.Thread(
                    target=self._execute_in_worker,
                    args=(item.pk, screen, results, errors),
                )
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(results, [(item.pk, 'pub_race_review')] * 2)
        self.assertEqual(PublicationAttempt.objects.get().state, 'published')
        self.assertEqual(PublicationBinding.objects.count(), 1)
        self.assertEqual(PublicationOutbox.objects.count(), 1)

    @skipUnless(connection.vendor == 'postgresql', 'unique-key concurrency requires PostgreSQL')
    def test_concurrent_same_key_different_carriers_returns_typed_operation_conflict(self):
        """§17.4 #4: per-carrier locks do not serialize a global operation key.
        The unique-key loser re-reads the winner after savepoint rollback and
        returns PUBLICATION_OPERATION_CONFLICT, never raw IntegrityError."""
        first = self._item('PUBR-2', 'race-round-2')
        second = self._item('PUBR-3', 'race-round-3')
        screens = {
            first.pk: self._screen('shared-race-key', 'race-round-2'),
            second.pk: self._screen('shared-race-key', 'race-round-3'),
        }
        insert_barrier = threading.Barrier(2)
        real_create = PublicationAttempt.objects.create

        def concurrent_create(*args, **kwargs):
            insert_barrier.wait(timeout=10)
            return real_create(*args, **kwargs)

        results = []
        errors = []
        with patch.object(
            PublicationAttempt.objects, 'create', side_effect=concurrent_create,
        ), patch(
            'tasks.services.publication_handoff._wiki_snapshot',
            return_value=self._receipt(),
        ), patch.object(deliver_publication_outbox, 'delay', return_value=None):
            threads = [
                threading.Thread(
                    target=self._execute_in_worker,
                    args=(candidate.pk, screens[candidate.pk], results, errors),
                )
                for candidate in (first, second)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], WorkflowValidationError)
        self.assertEqual(errors[0].code, 'PUBLICATION_OPERATION_CONFLICT')
        self.assertNotIsInstance(errors[0], IntegrityError)
        attempt = PublicationAttempt.objects.get(operation_key='shared-race-key')
        self.assertEqual(attempt.state, 'published')
        self.assertEqual(PublicationBinding.objects.count(), 1)
        self.assertEqual(PublicationOutbox.objects.count(), 1)
