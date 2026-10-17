"""
Tests for task assignment permissions.

Verifies that normal users (non-staff) can assign tasks to other users
within their organization, matching the production scenario where:
- User is a regular org member (not staff/admin)
- User wants to assign a task to another org member
- Both users are in the same organization and project
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from unittest.mock import patch, MagicMock
from io import StringIO

from tasks.models import Comment, Item, ItemActivity, Project, ProjectMembership, TeamRoster
from tasks.services.routing_authority import (
    clear_assignment_to_group_queue,
    reroute_assignment_to_group_queue,
)

User = get_user_model()

# All lazy imports of admin_service come from this module:
ADMIN_SERVICE_PATH = 'tasks.services.admin_service.admin_service'
# user_service is imported at module level in views.py:
USER_SERVICE_PATH = 'tasks.views.user_service'


class AssignmentPermissionTests(APITestCase):
    """Test that normal users can assign tasks within their org."""

    def setUp(self):
        """Set up two normal users in the same org and project."""
        self.client = APIClient()

        self.user_a = User.objects.create_user(
            username='user_a', email='user_a@example.com',
            password='testpass123', is_staff=False,
        )
        self.user_b = User.objects.create_user(
            username='user_b', email='user_b@example.com',
            password='testpass123', is_staff=False,
        )

        self.project = Project.objects.create(
            name='Test Project', key='TEST', org_id=1,
            access_level='open',
        )

        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user_a.id, user_email=self.user_a.email,
            role='member',
            can_browse=True, can_create=True, can_edit=True, can_delete=False,
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user_b.id, user_email=self.user_b.email,
            role='member',
            can_browse=True, can_create=True, can_edit=True, can_delete=False,
        )

        # Mock Admin before creating the assigned fixture: project membership is
        # only a project-local role and no longer substitutes for exact org
        # membership at the assignment write fence.
        self.admin_patcher = patch(ADMIN_SERVICE_PATH)
        self.mock_admin = self.admin_patcher.start()
        self.mock_admin.get_organization.return_value = {
            'id': 1, 'slug': 'shizuha', 'name': 'Shizuha',
        }
        self.mock_admin.is_org_member.return_value = True
        self.mock_admin.check_org_membership.return_value = {
            'is_member': True, 'role': 'member'
        }
        self.mock_admin.get_user_organization_ids.return_value = [1]
        self.mock_admin.get_user_organizations.return_value = [{'id': 1}]
        self.mock_admin.get_org_members.return_value = []

        self.task = Item.objects.create(
            mode='task', title='Task to reassign',
            project=self.project,
            workflow_name='simple',
            assignee_id=self.user_a.id, assignee_email=self.user_a.email,
            created_by_id=self.user_a.id, created_by_email=self.user_a.email,
        )

        # Mock user_service (module-level import in views.py)
        self.user_svc_patcher = patch(USER_SERVICE_PATH)
        self.mock_user_svc = self.user_svc_patcher.start()
        self.mock_user_svc.get_user_by_id.side_effect = lambda user_id: {
            self.user_a.id: {'id': self.user_a.id, 'email': self.user_a.email},
            self.user_b.id: {'id': self.user_b.id, 'email': self.user_b.email},
        }.get(user_id)

    def tearDown(self):
        self.admin_patcher.stop()
        self.user_svc_patcher.stop()

    def test_owner_can_reassign_task(self):
        """Test that the task owner (assignee) can reassign to another user."""
        self.client.force_authenticate(user=self.user_a)

        self.mock_user_svc.resolve_user_info.return_value = {
            'id': self.user_b.id, 'email': self.user_b.email,
            'username': 'user_b', 'full_name': 'User B', 'is_agent': False,
        }

        response = self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {'assignee_id': self.user_b.id, 'assignee_email': self.user_b.email, 'direct_assignment_reason': 'test direct assignment'},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.user_b.id)

    def test_assign_survives_resolve_user_info_missing_email(self):
        """PLS-381: resolve_user_info can return a truthy dict that LACKS an
        'email' key (a partial shizuha-id directory record for the assignee).
        The project-auto-add block read user_info['email'] directly and raised
        KeyError('email') -> a bare HTTP 500 (reproduced on PLAT-4006/assign).
        The assign endpoint must backfill the email from assignee_email and
        succeed instead of 500ing."""
        self.client.force_authenticate(user=self.user_a)
        # Partial record: id/username present, NO 'email' — the exact trigger.
        self.mock_user_svc.resolve_user_info.return_value = {
            'id': self.user_b.id, 'username': 'user_b', 'is_agent': False,
        }
        response = self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {'assignee_id': self.user_b.id, 'assignee_email': self.user_b.email,
             'direct_assignment_reason': 'PLS-381 regression'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_email, self.user_b.email)

    def test_assign_missing_email_no_backfill_does_not_500(self):
        """PLS-378 (same root cause as PLS-381, broader trigger): when the
        resolved record lacks 'email' AND there is no caller assignee_email to
        backfill from — the exact case behind the assignment_group resolver and
        the KOT-40/41 in_review->done 500s (a team-route member / phantom
        assignee whose shizuha-id record resolves partial) — the defensive read
        must skip the project-auto-add gracefully and NOT raise KeyError->500."""
        self.client.force_authenticate(user=self.user_a)
        # No 'email' on the resolved record; assignee_id only (no assignee_email)
        # so the backfill cannot fire — only the defensive .get('email') saves it.
        self.mock_user_svc.resolve_user_info.return_value = {
            'id': self.user_b.id, 'username': 'user_b', 'is_agent': False,
        }
        response = self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {'assignee_id': self.user_b.id, 'direct_assignment_reason': 'PLS-378 regression'},
            format='json',
        )
        # The bug was a bare HTTP 500 (KeyError('email')); the fix makes this
        # graceful — a success or a clean 4xx, never a 500.
        self.assertLess(response.status_code, 500, response.data)

    def test_project_member_can_reassign_others_task(self):
        """Test that a project member (non-owner) can reassign a task.

        This is the production bug scenario: user_b tries to reassign
        a task that belongs to user_a. user_b is a project member
        with can_edit=True in an open project.
        """
        self.client.force_authenticate(user=self.user_b)

        self.mock_user_svc.resolve_user_info.return_value = {
            'id': self.user_a.id, 'email': self.user_a.email,
            'username': 'user_a', 'full_name': 'User A', 'is_agent': False,
        }

        response = self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {'assignee_id': self.user_a.id, 'assignee_email': self.user_a.email, 'direct_assignment_reason': 'test direct assignment'},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_org_member_can_assign_in_open_project(self):
        """Test org member without explicit membership can assign in open project.

        For open projects, any org member should be able to edit/assign,
        even without explicit ProjectMembership.
        """
        user_c = User.objects.create_user(
            username='user_c', email='user_c@example.com',
            password='testpass123', is_staff=False,
        )
        # user_c has NO ProjectMembership but IS an org member (mocked above)
        self.client.force_authenticate(user=user_c)

        self.mock_user_svc.resolve_user_info.return_value = {
            'id': self.user_b.id, 'email': self.user_b.email,
            'username': 'user_b', 'full_name': 'User B', 'is_agent': False,
        }

        response = self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {'assignee_id': self.user_b.id, 'assignee_email': self.user_b.email, 'direct_assignment_reason': 'test direct assignment'},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_unassign_task(self):
        """Test that a project member can unassign a task."""
        self.client.force_authenticate(user=self.user_a)

        response = self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assignee_id)
        self.assertIsNone(self.task.assignee_email)

    def test_non_org_member_cannot_assign(self):
        """Test that a non-org member cannot assign tasks."""
        user_outsider = User.objects.create_user(
            username='outsider', email='outsider@example.com',
            password='testpass123', is_staff=False,
        )
        self.client.force_authenticate(user=user_outsider)

        # Non-org member: admin_service returns False
        self.mock_admin.is_org_member.return_value = False
        self.mock_admin.get_user_organization_ids.return_value = []
        self.mock_admin.get_user_organizations.return_value = []

        response = self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {'assignee_id': self.user_b.id},
            format='json'
        )

        # Should get 403 or 404 (object not found in queryset)
        self.assertIn(response.status_code, [
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        ])


class AtomicAssignmentModelTests(TestCase):
    """PLAT-192: assignee id/email writes derive from one canonical identity."""

    def test_assign_trusts_resolved_id_over_stale_email(self):
        item = Item.objects.create(mode='task', title='Atomic assignment', workflow_name='simple')

        with patch('tasks.user_service.user_service.get_user_by_id') as get_user_by_id:
            get_user_by_id.return_value = {'id': 42, 'email': 'canonical@shizuha.com'}
            self.assertTrue(item.assign({'id': 42, 'email': 'stale@shizuha.com'}, save=False))

        self.assertEqual(item.assignee_id, 42)
        self.assertEqual(item.assignee_email, 'canonical@shizuha.com')

    def test_clean_rejects_id_email_mismatch(self):
        item = Item(mode='task', title='Diverged assignment', workflow_name='simple',
                    assignee_id=42, assignee_email='stale@shizuha.com')

        with patch('tasks.user_service.user_service.get_user_by_id') as get_user_by_id:
            get_user_by_id.return_value = {'id': 42, 'email': 'canonical@shizuha.com'}
            with self.assertRaises(ValidationError):
                item.clean()

    def test_save_rejects_id_email_mismatch(self):
        item = Item(mode='task', title='Diverged assignment', workflow_name='simple',
                    assignee_id=42, assignee_email='stale@shizuha.com')

        with patch('tasks.user_service.user_service.get_user_by_id') as get_user_by_id:
            get_user_by_id.return_value = {'id': 42, 'email': 'canonical@shizuha.com'}
            with self.assertRaises(ValidationError):
                item.save()

    def test_pls378_partial_save_skips_unwritten_identity_pair(self):
        """PLS-378: the assign KeyError fix (PR #452) covered the /assign/ HTTP
        view, but the execute_transition 500 on KOT-40/41 was a DIFFERENT throw —
        item.save() re-validated the assignee/created_by identity pair on EVERY
        save, so a status-only transition raised a (DRF-unmapped -> HTTP 500)
        django ValidationError on those items' pre-existing id/email drift, leaving
        them permanently un-transitionable. A save whose update_fields don't
        include the identity columns must skip that pair; a save that DOES write
        the pair still enforces the invariant."""
        item = Item.objects.create(mode='task', title='Drifted', workflow_name='simple')
        # Inject drift directly (bypass validation on the initial create).
        Item.objects.filter(id=item.id).update(
            assignee_id=42, assignee_email='stale@shizuha.com')
        item.refresh_from_db()
        with patch('tasks.user_service.user_service.get_user_by_id') as get_user_by_id:
            get_user_by_id.return_value = {'id': 42, 'email': 'canonical@shizuha.com'}
            # Non-identity partial save -> identity pair NOT re-validated -> no raise.
            item.title = 'Drifted (transitioned)'
            item.save(update_fields=['title'])
            # A save that writes the assignee pair still enforces the invariant.
            with self.assertRaises(ValidationError):
                item.save(update_fields=['assignee_email'])

    def test_save_accepts_agent_alias_id_email(self):
        """PLAT-1049: ``<name>@shizuha.com`` and ``<name>@agents.shizuha.io`` are
        ONE identity, so a save (e.g. a status-only transition) of an item whose
        stored email is in the other alias domain must NOT raise. This previously
        500'd in execute_transition -> item.save(), leaving items like
        EVOL-22/KURA-4 (assignee banto@shizuha.com, canonical
        banto@agents.shizuha.io) permanently un-transitionable."""
        item = Item(mode='task', title='Alias assignment', workflow_name='simple',
                    assignee_id=112, assignee_email='banto@shizuha.com')

        with patch('tasks.user_service.user_service.get_user_by_id') as get_user_by_id:
            get_user_by_id.return_value = {'id': 112, 'email': 'banto@agents.shizuha.io'}
            item.save()  # must not raise

        item.refresh_from_db()
        self.assertEqual(item.assignee_id, 112)
        self.assertEqual(item.assignee_email, 'banto@shizuha.com')

    def test_save_accepts_agent_alias_reverse_direction(self):
        """Alias equivalence is symmetric and also covers created_by:
        stored @agents.shizuha.io, canonical @shizuha.com."""
        item = Item(mode='task', title='Alias reverse', workflow_name='simple',
                    created_by_id=88, created_by_email='ichi@agents.shizuha.io')

        with patch('tasks.user_service.user_service.get_user_by_id') as get_user_by_id:
            get_user_by_id.return_value = {'id': 88, 'email': 'ichi@shizuha.com'}
            item.save()  # must not raise

    def test_emails_same_identity_helper(self):
        same = Item._emails_same_identity
        # agent primary <-> internal alias (both directions), case-insensitive
        self.assertTrue(same('banto@shizuha.com', 'banto@agents.shizuha.io'))
        self.assertTrue(same('banto@agents.shizuha.io', 'banto@shizuha.com'))
        self.assertTrue(same('x@shizuha.com', 'X@SHIZUHA.COM'))
        # genuinely different agents are still rejected
        self.assertFalse(same('banto@shizuha.com', 'kai@agents.shizuha.io'))
        # arbitrary domains sharing a local-part are NOT collapsed
        self.assertFalse(same('banto@gmail.com', 'banto@outlook.com'))
        self.assertFalse(same('banto@shizuha.com', ''))

    def test_repair_command_trusts_id_for_mismatch_and_fills_half_resolved(self):
        mismatch = Item.objects.create(
            mode='task', title='Mismatch', workflow_name='simple',
            assignee_id=42, assignee_email='stale@shizuha.com',
        )
        created_by_mismatch = Item.objects.create(
            mode='task', title='Creator mismatch', workflow_name='simple',
            created_by_id=45, created_by_email='stale-creator@shizuha.com',
        )
        id_only = Item.objects.create(mode='task', title='Id only', workflow_name='simple', assignee_id=43)
        email_only = Item.objects.create(
            mode='task', title='Email only', workflow_name='simple', assignee_email='email-only@shizuha.com'
        )

        def by_id(uid):
            return {
                42: {'id': 42, 'email': 'canonical@shizuha.com'},
                43: {'id': 43, 'email': 'id-only@shizuha.com'},
                45: {'id': 45, 'email': 'creator@shizuha.com'},
            }.get(uid)

        def by_email(email):
            if email == 'email-only@shizuha.com':
                return {'id': 44, 'email': 'email-only@shizuha.com'}
            return None

        out = StringIO()
        with patch('tasks.management.commands.repair_assignee_identity_drift.user_service.get_user_by_id',
                   side_effect=by_id), \
             patch('tasks.management.commands.repair_assignee_identity_drift.user_service.get_user_by_email',
                   side_effect=by_email):
            call_command('repair_assignee_identity_drift', '--commit', '--field', 'both', stdout=out)

        mismatch.refresh_from_db()
        created_by_mismatch.refresh_from_db()
        id_only.refresh_from_db()
        email_only.refresh_from_db()
        self.assertEqual(mismatch.assignee_email, 'canonical@shizuha.com')
        self.assertEqual(created_by_mismatch.created_by_email, 'creator@shizuha.com')
        self.assertEqual(id_only.assignee_email, 'id-only@shizuha.com')
        self.assertEqual(email_only.assignee_id, 44)


class AssignmentInPrivateProjectTests(APITestCase):
    """Test assignment permissions in private projects."""

    def setUp(self):
        self.client = APIClient()

        self.user_a = User.objects.create_user(
            username='user_a', email='user_a@example.com',
            password='testpass123', is_staff=False,
        )
        self.user_b = User.objects.create_user(
            username='user_b', email='user_b@example.com',
            password='testpass123', is_staff=False,
        )

        self.project = Project.objects.create(
            name='Private Project', key='PRIV', org_id=1,
            access_level='private',
        )

        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user_a.id, user_email=self.user_a.email,
            role='member',
            can_browse=True, can_create=True, can_edit=True, can_delete=False,
        )

        self.admin_patcher = patch(ADMIN_SERVICE_PATH)
        self.mock_admin = self.admin_patcher.start()
        self.mock_admin.get_organization.return_value = {
            'id': 1, 'slug': 'shizuha', 'name': 'Shizuha',
        }
        self.mock_admin.is_org_member.return_value = True
        self.mock_admin.check_org_membership.return_value = {
            'is_member': True, 'role': 'member'
        }
        self.mock_admin.get_user_organization_ids.return_value = [1]
        self.mock_admin.get_user_organizations.return_value = [{'id': 1}]

        self.task = Item.objects.create(
            mode='task', title='Private task',
            project=self.project,
            workflow_name='simple',
            assignee_id=self.user_a.id, assignee_email=self.user_a.email,
            created_by_id=self.user_a.id, created_by_email=self.user_a.email,
        )

        self.user_svc_patcher = patch(USER_SERVICE_PATH)
        self.mock_user_svc = self.user_svc_patcher.start()
        self.mock_user_svc.get_user_by_id.side_effect = lambda user_id: {
            self.user_a.id: {'id': self.user_a.id, 'email': self.user_a.email},
            self.user_b.id: {'id': self.user_b.id, 'email': self.user_b.email},
        }.get(user_id)

    def tearDown(self):
        self.admin_patcher.stop()
        self.user_svc_patcher.stop()

    def test_member_can_assign_in_private_project(self):
        """Test project member with can_edit can assign in private project."""
        self.client.force_authenticate(user=self.user_a)

        self.mock_user_svc.resolve_user_info.return_value = {
            'id': self.user_b.id, 'email': self.user_b.email,
            'username': 'user_b', 'full_name': 'User B', 'is_agent': False,
        }

        response = self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {'assignee_id': self.user_b.id, 'assignee_email': self.user_b.email, 'direct_assignment_reason': 'test direct assignment'},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_non_member_cannot_access_private_project_task(self):
        """Test non-member cannot even see private project tasks."""
        self.client.force_authenticate(user=self.user_b)  # Not a project member

        response = self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {'assignee_id': self.user_a.id},
            format='json'
        )

        # Should be 404 (item not in queryset) or 403
        self.assertIn(response.status_code, [
            status.HTTP_403_FORBIDDEN,
            status.HTTP_404_NOT_FOUND,
        ])


class AssignmentByEmailTests(APITestCase):
    """Test that membership lookup works by email (not just user_id)."""

    def setUp(self):
        self.client = APIClient()

        self.user = User.objects.create_user(
            username='emailuser', email='emailuser@example.com',
            password='testpass123', is_staff=False,
        )

        self.project = Project.objects.create(
            name='Email Project', key='EMAIL', org_id=1,
            access_level='limited',
        )

        # Membership by email only (user_id is None - e.g., invited user)
        ProjectMembership.objects.create(
            project=self.project,
            user_id=None,
            user_email=self.user.email,
            role='member',
            can_browse=True, can_create=True, can_edit=True, can_delete=False,
        )

        self.task = Item.objects.create(
            mode='task', title='Email member task',
            project=self.project,
            workflow_name='simple',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )

        self.admin_patcher = patch(ADMIN_SERVICE_PATH)
        self.mock_admin = self.admin_patcher.start()
        self.mock_admin.get_organization.return_value = {
            'id': 1, 'slug': 'shizuha', 'name': 'Shizuha',
        }
        self.mock_admin.is_org_member.return_value = True
        self.mock_admin.check_org_membership.return_value = {
            'is_member': True, 'role': 'member'
        }
        self.mock_admin.get_user_organization_ids.return_value = [1]
        self.mock_admin.get_user_organizations.return_value = [{'id': 1}]

        self.user_svc_patcher = patch(USER_SERVICE_PATH)
        self.mock_user_svc = self.user_svc_patcher.start()
        self.mock_user_svc.get_user_by_id.side_effect = lambda user_id: {
            self.user.id: {'id': self.user.id, 'email': self.user.email},
            999: {'id': 999, 'email': 'someone@example.com'},
        }.get(user_id)

    def tearDown(self):
        self.admin_patcher.stop()
        self.user_svc_patcher.stop()

    def test_email_only_member_can_assign(self):
        """Test user with email-only membership can assign tasks.

        This covers the case where a user was invited by email and their
        ProjectMembership has user_id=None but user_email set.
        """
        self.client.force_authenticate(user=self.user)

        self.mock_user_svc.resolve_user_info.return_value = {
            'id': 999, 'email': 'someone@example.com',
            'username': 'someone', 'full_name': 'Someone', 'is_agent': False,
        }

        response = self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {
                'assignee_email': 'someone@example.com',
                'direct_assignment_reason': 'test direct email assignment',
            },
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)


class CreateAssignmentFinalityTests(APITestCase):
    """PLS-465: create commits and publishes one final assignment only."""

    def setUp(self):
        self.client = APIClient()
        self.creator = User.objects.create_user(
            username='p465_creator', email='creator@shizuha.com',
            password='x', is_staff=True,
        )
        self.direct_owner = User.objects.create_user(
            username='p465_hiro', email='hiro@shizuha.com',
            password='x', is_staff=False,
        )
        self.team_owner = User.objects.create_user(
            username='p465_kai', email='kai@shizuha.com',
            password='x', is_staff=False,
        )
        self.project = Project.objects.create(
            name='PLS-465', key='P465', org_id=1, access_level='open',
        )
        for team_name, email in (
            ('review', self.direct_owner.email),
            ('engineering', self.team_owner.email),
        ):
            TeamRoster.objects.update_or_create(
                organization_slug='shizuha',
                team_name=team_name,
                defaults={
                    'organization_id': self.project.org_id,
                    'tiers': [[email]],
                    'is_active': True,
                },
            )
        for user in (self.creator, self.direct_owner, self.team_owner):
            ProjectMembership.objects.create(
                project=self.project,
                user_id=user.id,
                user_email=user.email,
                role='member',
                can_browse=True,
                can_create=True,
                can_edit=True,
                can_delete=False,
            )
        self.client.force_authenticate(user=self.creator)

        known = (self.creator, self.direct_owner, self.team_owner)

        def by_email(email):
            target = (email or '').strip().lower()
            for user in known:
                if user.email.lower() == target:
                    return {'id': user.id, 'email': user.email}
            return None

        def by_id(user_id):
            for user in known:
                if int(user_id) == user.id:
                    return {'id': user.id, 'email': user.email}
            return None

        self.directory_patches = [
            patch('tasks.user_service.user_service.get_user_by_email', side_effect=by_email),
            patch('tasks.user_service.user_service.get_user_by_id', side_effect=by_id),
            patch('tasks.services.workflow_service.WorkflowService._is_known_team', return_value=True),
            patch('tasks.services.workflow_service.WorkflowService.assignment_org_mismatch',
                  return_value=(False, None, None)),
        ]
        for patcher in self.directory_patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _create(self, **assignment):
        body = {
            'title': f'PLS-465 create {Item.objects.count()}',
            'mode': 'task',
            'project': self.project.id,
            'workflow_name': 'simple',
        }
        body.update(assignment)
        return self.client.post('/api/items/', body, format='json')

    @patch('tasks.tasks.send_item_notification.delay')
    def test_direct_create_records_and_notifies_only_explicit_owner(self, delay):
        with patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee'
        ) as resolver, self.captureOnCommitCallbacks(execute=True):
            response = self._create(
                assignment_group='review',
                assignee_email=self.direct_owner.email,
                direct_assignment_reason='SCLI-338 exact-version continuity',
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        item = Item.objects.get(pk=response.data['id'])
        self.assertEqual(item.assignee_id, self.direct_owner.id)
        self.assertEqual(item.assignee_email, self.direct_owner.email)
        self.assertEqual(item.assignment_group, 'review')
        resolver.assert_not_called()
        assigned = ItemActivity.objects.filter(item=item, action='assigned')
        self.assertEqual(assigned.count(), 1)
        self.assertEqual(assigned.get().new_value, self.direct_owner.email)
        delay.assert_called_once()
        args = delay.call_args.args
        self.assertEqual(args[:2], (str(item.pk), 'assignment_changed'))
        self.assertEqual(args[2]['dispatched_assignee_id'], str(self.direct_owner.id))

    @patch('tasks.tasks.send_item_notification.delay')
    def test_team_only_create_resolves_records_and_notifies_once(self, delay):
        with patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
            return_value=(self.team_owner.id, self.team_owner.email),
        ), self.captureOnCommitCallbacks(execute=True):
            response = self._create(assignment_group='engineering')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        item = Item.objects.get(pk=response.data['id'])
        self.assertEqual(item.assignee_id, self.team_owner.id)
        self.assertEqual(item.assignee_email, self.team_owner.email)
        self.assertEqual(
            ItemActivity.objects.filter(item=item, action='assigned').count(), 1,
        )
        delay.assert_called_once()
        self.assertEqual(
            delay.call_args.args[2]['dispatched_assignee_id'],
            str(self.team_owner.id),
        )

    @patch('tasks.tasks.send_item_notification.delay')
    def test_direct_create_without_reason_is_atomic_rejection(self, delay):
        # A DIRECT create (a concrete assignee, no team route) with NO reason is
        # rejected with zero mutation. Under the ONE uniform precedence a group is the
        # team-route signal, so the direct case is an assignee with NO co-supplied
        # group (group+assignee+NO reason is a team route, covered separately in
        # PLS539CrossEntrypointDirectHandoffTests).
        before = Item.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            response = self._create(
                assignee_email=self.direct_owner.email,
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertEqual(Item.objects.count(), before)
        delay.assert_not_called()


class OperatorAssignmentGuardTests(APITestCase):
    """PLS-16: the operator (CEO) is LAST RESORT — only the Chief of Staff
    (Shizuha) may hand him a task; agents must route through her, not assign him
    directly, so the escalation chain can't be bypassed."""

    OPERATOR = 'operator@example.test'
    COS = 'shizuha@shizuha.com'

    def setUp(self):
        self.client = APIClient()
        self.agent = User.objects.create_user(
            username='ryo', email='ryo@shizuha.com', password='x', is_staff=False)
        self.cos = User.objects.create_user(
            username='shizuha', email=self.COS, password='x', is_staff=False)
        self.operator = User.objects.create_user(
            username='hritik', email=self.OPERATOR, password='x', is_staff=False)

        self.project = Project.objects.create(
            name='P', key='OPS', org_id=1, access_level='open')
        for u in (self.agent, self.cos, self.operator):
            ProjectMembership.objects.create(
                project=self.project, user_id=u.id, user_email=u.email,
                role='member', can_browse=True, can_create=True,
                can_edit=True, can_delete=False)
            # can_access_project reads org membership from the authenticated user
            # (carried by the JWT in prod); force_authenticate doesn't set it, so
            # a non-staff CoS would be denied create on the org's project. Grant it.
            u.organization_memberships = {self.project.org_id: 'member'}

        self.admin_patcher = patch(ADMIN_SERVICE_PATH)
        self.mock_admin = self.admin_patcher.start()
        self.addCleanup(self.admin_patcher.stop)
        self.mock_admin.is_org_member.return_value = True
        self.mock_admin.check_org_membership.return_value = {
            'is_member': True,
            'role': 'member',
        }

        self.task = Item.objects.create(
            mode='task', title='T', project=self.project,
            workflow_name='simple',
            assignee_id=self.agent.id, assignee_email=self.agent.email,
            created_by_id=self.agent.id, created_by_email=self.agent.email)

        # Item create resolves assignee_email->id via the user_service singleton
        # (the shizuha-id directory), unreachable in tests -> the CoS create would
        # 400 on assignee resolution even though the operator-gate allows it. Patch
        # the singleton's lookups to resolve these local test users.
        from tasks.user_service import user_service as _us
        _known = (self.agent, self.cos, self.operator)

        def _by_email(email):
            e = (email or '').strip().lower()
            for u in _known:
                if u.email.lower() == e:
                    return {'id': u.id, 'email': u.email}
            return None

        def _by_id(uid):
            for u in _known:
                if int(uid) == u.id:
                    return {'id': u.id, 'email': u.email}
            return None

        patch.object(_us, 'get_user_by_email', side_effect=_by_email).start()
        patch.object(_us, 'get_user_by_id', side_effect=_by_id).start()
        self.addCleanup(patch.stopall)

        self.admin_patcher = patch(ADMIN_SERVICE_PATH)
        self.mock_admin = self.admin_patcher.start()
        self.mock_admin.get_organization.return_value = {
            'id': 1, 'slug': 'shizuha', 'name': 'Shizuha',
        }
        self.mock_admin.is_org_member.return_value = True
        self.mock_admin.check_org_membership.return_value = {'is_member': True, 'role': 'member'}
        self.mock_admin.get_user_organization_ids.return_value = [1]
        self.mock_admin.get_user_organizations.return_value = [{'id': 1}]
        self.mock_admin.get_org_members.return_value = []
        self.user_svc_patcher = patch(USER_SERVICE_PATH)
        self.mock_user_svc = self.user_svc_patcher.start()
        self.mock_user_svc.resolve_user_info.return_value = {
            'id': self.operator.id, 'email': self.OPERATOR,
            'username': 'hritik', 'full_name': 'Operator', 'is_agent': False,
        }
        self.mock_user_svc.get_user_by_id.return_value = {
            'id': self.operator.id, 'email': self.OPERATOR,
            'username': 'hritik', 'full_name': 'Operator', 'is_agent': False,
        }
        # PLS-231: the CoS->operator PATCH path resolves the assignee by email
        # (Item._resolve_user_ref -> user_service.get_user_by_email). Without this
        # mock the call returned a MagicMock that propagated into the saved field/
        # response and ballooned the allocation (the 12GB OOM -> 500). Mock it to the
        # real operator dict so resolution yields a plain (id, email).
        self.mock_user_svc.get_user_by_email.return_value = {
            'id': self.operator.id, 'email': self.OPERATOR,
            'username': 'hritik', 'full_name': 'Operator', 'is_agent': False,
        }

    def tearDown(self):
        self.admin_patcher.stop()
        self.user_svc_patcher.stop()

    def _assign_operator(self, **extra):
        body = {'assignee_id': self.operator.id, 'assignee_email': self.OPERATOR}
        body.update(extra)
        return self.client.post(f'/api/items/{self.task.id}/assign/', body, format='json')

    def _assign_operator_by_id_only(self, actor):
        self.client.force_authenticate(user=actor)
        return self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {'assignee_id': self.operator.id, 'direct_assignment_reason': 'please look'},
            format='json',
        )

    def _patch_operator_task(self, actor, **assignee_fields):
        self.client.force_authenticate(user=actor)
        return self.client.patch(f'/api/items/{self.task.id}/', assignee_fields, format='json')

    def test_agent_cannot_assign_to_operator(self):
        """A non-CoS agent assigning a non-merge task to the operator is rejected."""
        self.client.force_authenticate(user=self.agent)
        resp = self._assign_operator(direct_assignment_reason='please look')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('LAST', str(resp.data).upper())

    @patch(
        'tasks.services.workflow_service.WorkflowService._is_agent_available_for_assignment',
        return_value=True,
    )
    def test_pls747_team_routed_ceo_decisions_retains_rostered_operator(self, _available):
        """Production-order ``/assign/`` regression for ADR-0058.

        This arm intentionally uses a populated operator-only roster: the team
        resolver elects the operator, then the HTTP assignment path must retain
        that team-selected subject rather than rewrite it to the CoS and trip
        the PLAT-4213 exact-team guard.  The direct-person ban is covered by the
        adjacent tests and remains unchanged.
        """
        TeamRoster.objects.update_or_create(
            organization_id=self.project.org_id,
            team_name='ceo-decisions',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.OPERATOR]],
                'lead_email': self.OPERATOR,
                'is_active': True,
            },
        )
        from tasks.services.workflow_service import WorkflowService
        WorkflowService._invalidate_team_roster_caches()

        def _lookup_by_email(email):
            normalized = (email or '').strip().lower()
            if normalized == self.OPERATOR:
                user = self.operator
            elif normalized == self.COS:
                user = self.cos
            else:
                return None
            return {'id': user.id, 'email': user.email}

        self.mock_user_svc.get_user_by_email.side_effect = _lookup_by_email

        self.client.force_authenticate(user=self.agent)
        response = self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {'assignment_group': 'ceo-decisions'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignment_group, 'ceo-decisions')
        self.assertEqual(self.task.assignee_id, self.operator.id)
        self.assertEqual(self.task.assignee_email, self.OPERATOR)

    def test_agent_cannot_assign_awaiting_merge_to_operator(self):
        """PLS-16 removes the legacy awaiting_merge bypass; agents still route via CoS."""
        self.task.status = 'awaiting_merge'
        self.task.save(update_fields=['status'])
        self.client.force_authenticate(user=self.agent)
        resp = self._assign_operator(direct_assignment_reason='merge please')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('CHIEF OF STAFF', str(resp.data).upper())

    def test_agent_cannot_assign_to_operator_by_id_only(self):
        """The custom assign guard resolves assignee_id so omitting email is not a bypass."""
        resp = self._assign_operator_by_id_only(self.agent)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('LAST', str(resp.data).upper())

    def test_cos_can_assign_to_operator_by_id_only(self):
        """The Chief of Staff may assign the operator even when the UI sends only assignee_id."""
        resp = self._assign_operator_by_id_only(self.cos)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.task.refresh_from_db()
        self.assertEqual((self.task.assignee_email or '').lower(), self.OPERATOR)

    def test_agent_cannot_patch_operator_assignment_by_email_at_awaiting_merge(self):
        """PATCH follows PLS-16 too: no legacy awaiting_merge bypass."""
        self.task.status = 'awaiting_merge'
        self.task.save(update_fields=['status'])
        resp = self._patch_operator_task(self.agent, assignee_email=self.OPERATOR)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('LAST', str(resp.data).upper())

    def test_agent_cannot_patch_operator_assignment_by_id(self):
        """PATCH resolves assignee_id so omitting assignee_email is not a bypass."""
        resp = self._patch_operator_task(self.agent, assignee_id=self.operator.id)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('LAST', str(resp.data).upper())

    def test_cos_can_patch_operator_assignment(self):
        """The CoS gateway may use the standard PATCH path to place work on the operator.

        PLAT-4871/PLS-539: the direct-handoff reason contract is now identical across
        create, /assign and PATCH (exempt only when the ACTOR is the operator), so a
        CoS→operator direct handoff carries a reason on every entry point — matching
        the /assign + create operator tests, which already pass one."""
        resp = self._patch_operator_task(
            self.cos, assignee_email=self.OPERATOR,
            direct_assignment_reason='CoS escalation: place work on the operator')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.task.refresh_from_db()
        self.assertEqual((self.task.assignee_email or '').lower(), self.OPERATOR)

    def test_cos_patch_operator_does_not_propagate_mock_or_balloon_response(self):
        """PLS-231 regression: CoS->operator PATCH must resolve to a bounded 200.

        The 12GB OOM (exit 137) came from user_service.get_user_by_email returning a
        MagicMock that propagated into the saved assignee_email and the serialized
        response, whose repr/format chain ballooned the allocation. Guard both the
        saved field (a plain str, not a mock repr) and the response size.
        """
        import json
        resp = self._patch_operator_task(
            self.cos, assignee_email=self.OPERATOR,
            direct_assignment_reason='CoS escalation: place work on the operator')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.task.refresh_from_db()
        saved = self.task.assignee_email
        self.assertIsInstance(saved, str)
        self.assertEqual(saved.lower(), self.OPERATOR)
        self.assertNotIn('MagicMock', saved)
        self.assertNotIn('Mock', saved)
        # Size assertion: a well-formed item response is a few KB. A runaway
        # (mock repr / recursive object) would be orders of magnitude larger.
        body = json.dumps(resp.data, default=str)
        self.assertLess(len(body), 50_000,
                        f"response body ballooned to {len(body)} bytes — runaway resolution regressed")

    def test_resolve_user_ref_rejects_non_dict_from_user_service(self):
        """PLS-231 unit guard: a non-mapping user_service result never propagates.

        The exact failure mode: user_service.get_user_by_email returns a truthy
        non-dict (a MagicMock in the failing test, or a malformed upstream payload).
        _resolve_user_ref must fail closed to (None, None) rather than returning a
        mock whose downstream str()/format chain OOMs the process.
        """
        from unittest.mock import MagicMock
        for garbage in (MagicMock(), object(), 'raw-string', 42, ['x'], (1, 2)):
            self.mock_user_svc.get_user_by_email.return_value = garbage
            uid, email = Item._resolve_user_ref('someone@example.com')
            self.assertIsNone(uid, f"non-dict {type(garbage).__name__} leaked an id")
            self.assertIsNone(email, f"non-dict {type(garbage).__name__} leaked an email")

    def test_operator_can_patch_self_assign(self):
        """The operator may self-assign through the standard PATCH path."""
        resp = self._patch_operator_task(self.operator, assignee_email=self.OPERATOR)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.task.refresh_from_db()
        self.assertEqual((self.task.assignee_email or '').lower(), self.OPERATOR)

    def test_cos_can_assign_to_operator(self):
        """The Chief of Staff is the escalation gateway — she may assign the operator."""
        self.client.force_authenticate(user=self.cos)
        resp = self._assign_operator(direct_assignment_reason='shareholder decision: billing')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.task.refresh_from_db()
        self.assertEqual((self.task.assignee_email or '').lower(), self.OPERATOR)

    def test_operator_self_assign_allowed(self):
        """The operator may always self-assign (e.g. from the UI)."""
        self.client.force_authenticate(user=self.operator)
        resp = self._assign_operator()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def _create_operator_task(self, actor, **assignee_fields):
        self.client.force_authenticate(user=actor)
        body = {
            'title': 'Escalation bypass attempt',
            'mode': 'task',
            'project': self.project.id,
            'workflow_name': 'simple',
            'direct_assignment_reason': 'shareholder decision needed',
        }
        body.update(assignee_fields)
        return self.client.post('/api/items/', body, format='json')

    def test_agent_cannot_create_task_assigned_to_operator_by_email(self):
        """Create-with-assignee cannot bypass the operator-assignment chain."""
        resp = self._create_operator_task(self.agent, assignee_email=self.OPERATOR)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('LAST', str(resp.data).upper())

    def test_agent_cannot_create_task_assigned_to_operator_by_id(self):
        """The create guard resolves assignee_id so omitting email is not a bypass."""
        resp = self._create_operator_task(self.agent, assignee_id=self.operator.id)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('LAST', str(resp.data).upper())

    def test_cos_can_create_task_assigned_to_operator(self):
        """The Chief of Staff remains the gateway that may place work on the operator."""
        resp = self._create_operator_task(self.cos, assignee_email=self.OPERATOR)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        item = Item.objects.get(title='Escalation bypass attempt')
        self.assertEqual((item.assignee_email or '').lower(), self.OPERATOR)


class Plat4178KnownTeamGroupOnlyTests(APITestCase):
    """PLAT-4178: `/api/items/{id}/assign/` must NEVER 400 for a KNOWN team.

    When a known team resolves to no concrete member — members at the WIP cap,
    all currently UNAVAILABLE (offline/disabled), or genuinely zero-member — the
    endpoint must accept the task GROUP-ONLY so it stays team-routed (pulled when a
    member frees), not strand it unrouted in Needs Triage. The one exception is an
    in-flight (working-status) task, which must keep a concrete owner (orphan-safety)
    and gets a 409 rather than being group-orphaned.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='p4178', email='p4178@example.com', password='x', is_staff=True,
        )
        self.project = Project.objects.create(
            name='P4178', key='P4178', org_id=1, access_level='open',
        )
        ProjectMembership.objects.create(
            project=self.project, user_id=self.user.id, user_email=self.user.email,
            role='member', can_browse=True, can_create=True, can_edit=True, can_delete=True,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.user.email]],
                'is_active': True,
            },
        )
        self.admin_patcher = patch(ADMIN_SERVICE_PATH)
        m = self.admin_patcher.start()
        m.is_org_member.return_value = True
        m.check_org_membership.return_value = {'is_member': True, 'role': 'member'}
        m.get_user_organization_ids.return_value = [1]
        m.get_user_organizations.return_value = [{'id': 1}]
        m.get_org_members.return_value = []
        self.user_svc_patcher = patch(USER_SERVICE_PATH)
        self.user_svc_patcher.start()
        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        self.admin_patcher.stop()
        self.user_svc_patcher.stop()

    def _task(self, status_value, *, owned=False):
        item = Item.objects.create(
            mode='task', title='p4178 task', project=self.project, status=status_value,
            workflow_name='simple',
            created_by_id=self.user.id, created_by_email=self.user.email,
        )
        if owned:
            item.assignee_id = self.user.id
            item.assignee_email = self.user.email
            item.save(update_fields=['assignee_id', 'assignee_email'])
        return item

    @patch('tasks.services.workflow_service.WorkflowService._maybe_file_zero_member_team_alert', return_value=None)
    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee', return_value=(None, None))
    @patch('tasks.services.workflow_service.WorkflowService._is_known_team', return_value=True)
    def test_known_team_no_available_member_accepts_group_only_not_400(self, *_):
        task = self._task('open')  # not in-flight
        resp = self.client.post(
            f'/api/items/{task.id}/assign/',
            {'assignment_group': 'engineering'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(resp.data.get('assignment_group'), 'engineering')
        self.assertIsNone(resp.data.get('assignee_email'))
        self.assertTrue(resp.data.get('queued_no_available_member'))
        task.refresh_from_db()
        self.assertEqual(task.assignment_group, 'engineering')  # team-routed, not stranded
        self.assertIsNone(task.assignee_id)

    @patch('tasks.services.workflow_service.WorkflowService._maybe_file_zero_member_team_alert', return_value=None)
    @patch('tasks.services.workflow_service.WorkflowService._cap_applies', return_value=True)
    @patch(
        'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
        side_effect=lambda *args, **kwargs: (
            (None, None) if kwargs.get('enforce_cap') else (88, 'ichi@shizuha.com')
        ),
    )
    @patch('tasks.services.workflow_service.WorkflowService._is_known_team', return_value=True)
    def test_same_lane_capacity_queue_clears_owner_without_reroute_or_500(self, *_):
        """PLS-492: all-capped Engineering stays in-lane and uses CLEAR once."""
        task = self._task('open', owned=True)
        task.assignment_group = 'engineering'
        task.save(update_fields=['assignment_group'])

        with patch(
            'tasks.services.routing_authority.clear_assignment_to_group_queue',
            wraps=clear_assignment_to_group_queue,
        ) as clear_command, patch(
            'tasks.services.routing_authority.reroute_assignment_to_group_queue',
            wraps=reroute_assignment_to_group_queue,
        ) as reroute_command:
            resp = self.client.post(
                f'/api/items/{task.id}/assign/',
                {'assignment_group': 'engineering'}, format='json',
            )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertTrue(resp.data.get('queued_at_capacity'))
        task.refresh_from_db()
        self.assertEqual(task.assignment_group, 'engineering')
        self.assertIsNone(task.assignee_id)
        self.assertIsNone(task.assignee_email)
        clear_command.assert_called_once_with(task, reason='no_candidate', save=False)
        reroute_command.assert_not_called()

    @patch('tasks.services.workflow_service.WorkflowService._maybe_file_zero_member_team_alert', return_value=None)
    @patch('tasks.services.workflow_service.WorkflowService._cap_applies', return_value=True)
    @patch(
        'tasks.services.workflow_service.WorkflowService._resolve_team_assignee',
        side_effect=lambda *args, **kwargs: (
            (None, None) if kwargs.get('enforce_cap', True)
            else (88, 'security-owner@example.com')
        ),
    )
    @patch('tasks.services.workflow_service.WorkflowService._is_known_team', return_value=True)
    def test_security_review_valid_owner_survives_two_no_candidate_retries(self, *_):
        """PLS-591: two capped Security re-assertions retain a valid owner."""
        security_owner = User.objects.create_user(
            username='security-owner',
            email='security-owner@example.com',
            password='x',
            is_staff=True,
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=security_owner.id,
            user_email=security_owner.email,
            role='member',
            can_browse=True,
            can_create=True,
            can_edit=True,
            can_delete=True,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='security',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[security_owner.email]],
                'is_active': True,
            },
        )
        task = self._task('in_review')
        task.priority = 'high'
        task.assignment_group = 'security'
        task.assignee_id = security_owner.id
        task.assignee_email = security_owner.email
        task.save(update_fields=[
            'priority', 'assignment_group', 'assignee_id', 'assignee_email',
        ])
        # This fixture uses the legacy ``simple`` workflow, whose model save
        # coerces unknown stages. Persist the production review stage directly
        # so the API exercises the real in-review handoff boundary.
        Item.objects.filter(pk=task.pk).update(
            workflow_name='Bug Workflow',
            status='in_review',
        )
        task.refresh_from_db()

        with patch(
            'tasks.services.routing_authority.clear_assignment_to_group_queue',
            wraps=clear_assignment_to_group_queue,
        ) as clear_command, patch(
            'tasks.services.routing_authority.reroute_assignment_to_group_queue',
            wraps=reroute_assignment_to_group_queue,
        ) as reroute_command:
            responses = [
                self.client.post(
                    f'/api/items/{task.id}/assign/',
                    {'assignment_group': 'security'}, format='json',
                )
                for _ in range(2)
            ]

        for resp in responses:
            self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        task.refresh_from_db()
        self.assertEqual(task.assignment_group, 'security')
        self.assertEqual(task.assignee_id, security_owner.id)
        self.assertEqual(task.assignee_email, security_owner.email)
        self.assertEqual(
            Comment.objects.filter(
                item=task,
                origin_event='resolver.review_starvation',
            ).count(),
            0,
        )
        self.assertFalse(
            Comment.objects.filter(
                item=task,
                content__contains='security review → review board',
            ).exists(),
        )
        clear_command.assert_not_called()
        reroute_command.assert_not_called()
        self.assertTrue(all(
            response.data.get('preserved_same_role_owner')
            for response in responses
        ))

    @patch('tasks.services.workflow_service.WorkflowService._maybe_file_zero_member_team_alert', return_value=None)
    @patch('tasks.services.workflow_service.WorkflowService._cap_applies', return_value=False)
    @patch('tasks.services.workflow_service.WorkflowService._member_can_execute_from_status', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._seat_satisfies_required_capability', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._is_team_member', return_value=True)
    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee', return_value=(None, None))
    @patch('tasks.services.workflow_service.WorkflowService._is_known_team', return_value=True)
    def test_review_owner_survives_transient_author_evidence_failure(self, *_):
        """PLS-902: provider failure cannot erase an authorized reviewer."""
        from tasks.services.workflow_service import WorkflowService

        reviewer = User.objects.create_user(
            username='review-owner',
            email='review-owner@example.com',
            password='x',
            is_staff=True,
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=reviewer.id,
            user_email=reviewer.email,
            role='member',
            can_browse=True,
            can_create=True,
            can_edit=True,
            can_delete=True,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='review',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[reviewer.email]],
                'is_active': True,
            },
        )
        task = self._task('in_review')
        task.priority = 'high'
        task.assignment_group = 'review'
        task.assignee_id = reviewer.id
        task.assignee_email = reviewer.email
        task.metadata = {
            'pull_requests': [{
                'url': (
                    'https://origin.shizuha.com/shizuha-labs/'
                    'shizuha-tasks/pulls/230'
                ),
                'status': 'open',
                'head_sha': 'stored-head-before-follow-up-push',
            }],
        }
        task.save(update_fields=[
            'priority', 'assignment_group', 'assignee_id', 'assignee_email',
            'metadata',
        ])
        Item.objects.filter(pk=task.pk).update(
            workflow_name='Bug Workflow',
            status='in_review',
        )
        task.refresh_from_db()

        author_lookup = patch.object(
            WorkflowService,
            '_linked_origin_pr_authors',
            return_value=(set(), ['origin_provider_unavailable'], 1),
        )
        with author_lookup as lookup, patch(
            'tasks.services.routing_authority.clear_assignment_to_group_queue',
            wraps=clear_assignment_to_group_queue,
        ) as clear_command, patch(
            'tasks.services.routing_authority.reroute_assignment_to_group_queue',
            wraps=reroute_assignment_to_group_queue,
        ) as reroute_command:
            # A fresh candidate still fails closed without exact author evidence.
            self.assertIn(
                'review_sod_author_evidence_unavailable',
                WorkflowService._review_role_hard_policy_reasons(
                    task,
                    'review',
                    'new-reviewer@example.com',
                ),
            )
            responses = [
                self.client.post(
                    f'/api/items/{task.id}/assign/',
                    {'assignment_group': 'review'},
                    format='json',
                )
                for _ in range(2)
            ]

        for response in responses:
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.assertTrue(response.data.get('preserved_same_role_owner'))
        task.refresh_from_db()
        self.assertEqual(task.assignment_group, 'review')
        self.assertEqual(task.assignee_id, reviewer.id)
        self.assertEqual(task.assignee_email, reviewer.email)
        self.assertGreaterEqual(lookup.call_count, 3)
        clear_command.assert_not_called()
        reroute_command.assert_not_called()

    @patch('tasks.services.workflow_service.WorkflowService._maybe_file_zero_member_team_alert', return_value=None)
    @patch('tasks.services.workflow_service.WorkflowService._cap_applies', return_value=False)
    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee', return_value=(None, None))
    @patch('tasks.services.workflow_service.WorkflowService._is_known_team', return_value=True)
    def test_security_review_never_retains_task_creator_as_owner(self, *_):
        """PLS-591: owner retention cannot bypass no-self-review."""
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='security',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.user.email]],
                'is_active': True,
            },
        )
        task = self._task('in_review', owned=True)
        task.priority = 'high'
        task.assignment_group = 'security'
        task.save(update_fields=['priority', 'assignment_group'])
        Item.objects.filter(pk=task.pk).update(
            workflow_name='Bug Workflow',
            status='in_review',
        )

        responses = [
            self.client.post(
                f'/api/items/{task.id}/assign/',
                {'assignment_group': 'security'},
                format='json',
            )
            for _ in range(2)
        ]

        for response in responses:
            self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
            self.assertFalse(response.data.get('preserved_same_role_owner', False))
        task.refresh_from_db()
        self.assertEqual(task.assignment_group, 'security')
        self.assertIsNone(task.assignee_id)
        self.assertFalse(task.assignee_email)

    @patch('tasks.services.workflow_service.WorkflowService._maybe_file_zero_member_team_alert', return_value=None)
    @patch('tasks.services.workflow_service.WorkflowService._cap_applies', return_value=False)
    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee', return_value=(None, None))
    @patch('tasks.services.workflow_service.WorkflowService._is_known_team', return_value=True)
    def test_security_review_cross_rostered_prior_role_owner_is_cleared(self, *_):
        """PLS-591: cross-role eligibility cannot authorize a fresh handoff."""
        from tasks.services.workflow_service import WorkflowService

        security_owner = User.objects.create_user(
            username='cross-rostered-security-owner',
            email='cross-rostered-security-owner@example.com',
            password='x',
            is_staff=True,
        )
        ProjectMembership.objects.create(
            project=self.project,
            user_id=security_owner.id,
            user_email=security_owner.email,
            role='member',
            can_browse=True,
            can_create=True,
            can_edit=True,
            can_delete=True,
        )
        # Persist the owner on its legitimate prior Engineering lane.  The
        # canonical roster model deliberately rejects dual-roster fixtures, so
        # the request below stubs the destination hard-policy verdict to the
        # exact state a legacy/cross-rostered owner would produce: every
        # Security eligibility/SoD facet passes, but the prior lane is still
        # Engineering.  This keeps the real Item.save + /assign sequence while
        # isolating the pre-mutation-group boundary under regression.
        TeamRoster.objects.filter(
            organization_id=1,
            team_name='engineering',
        ).update(tiers=[[security_owner.email]])
        WorkflowService._invalidate_team_roster_caches()
        task = self._task('in_review')
        task.priority = 'high'
        task.assignment_group = 'engineering'
        task.assignee_id = security_owner.id
        task.assignee_email = security_owner.email
        task.save(update_fields=[
            'priority', 'assignment_group', 'assignee_id', 'assignee_email',
        ])
        Item.objects.filter(pk=task.pk).update(
            workflow_name='Bug Workflow',
            status='in_review',
        )
        task.refresh_from_db()

        with patch.object(
            WorkflowService,
            '_review_role_hard_policy_reasons',
            return_value=(),
        ) as hard_policy, patch(
            'tasks.services.routing_authority.clear_assignment_to_group_queue',
            wraps=clear_assignment_to_group_queue,
        ) as clear_command, patch(
            'tasks.services.routing_authority.reroute_assignment_to_group_queue',
            wraps=reroute_assignment_to_group_queue,
        ) as reroute_command:
            # Prove that every destination hard-policy facet would retain this
            # owner if it had already belonged to the Security lane.  The real
            # /assign call must nevertheless clear it because the captured
            # pre-handoff lane is Engineering.
            self.assertTrue(
                WorkflowService._has_valid_current_exact_role_owner(
                    task,
                    'security',
                    prior_assignment_group='security',
                )
            )
            response = self.client.post(
                f'/api/items/{task.id}/assign/',
                {'assignment_group': 'security'}, format='json',
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        task.refresh_from_db()
        self.assertEqual(task.assignment_group, 'security')
        self.assertIsNone(task.assignee_id)
        self.assertIsNone(task.assignee_email)
        clear_command.assert_not_called()
        self.assertEqual(reroute_command.call_count, 1)
        self.assertEqual(reroute_command.call_args.args[1], 'security')
        self.assertEqual(reroute_command.call_args.kwargs, {
            'reason': 'no_candidate', 'save': False,
        })
        hard_policy.assert_called_once()

    @patch('tasks.services.workflow_service.WorkflowService._maybe_file_zero_member_team_alert', return_value=None)
    @patch('tasks.services.workflow_service.WorkflowService._resolve_team_assignee', return_value=(None, None))
    @patch('tasks.services.workflow_service.WorkflowService._is_known_team', return_value=True)
    def test_in_flight_task_preserves_owner_not_group_orphaned(self, *_):
        task = self._task('in_progress', owned=True)  # in-flight, has a concrete owner
        resp = self.client.post(
            f'/api/items/{task.id}/assign/',
            {'assignment_group': 'engineering'}, format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT, resp.data)
        task.refresh_from_db()
        self.assertEqual(task.assignee_id, self.user.id)  # current owner preserved


class BareAssignUnchangedContextTests(APITestCase):
    """PLS-1339 (re-break of PLS-487): a bare/unchanged-context POST to
    /assign/ on an already-team-routed unstarted item must retain the
    assignment_group (in-lane CLEAR to the group-only queue) — never null the
    group and return a silent empty-200 that strands the carrier off every
    roster. Reconciled with the PLAT-4572 contract: a bare POST on an item
    WITH a concrete owner remains the explicit unassign command (group
    nulls); the retain-path applies to OWNERLESS group-routed rows only,
    where unassign is a no-op probe. A genuinely ungrouped item still
    unassigns cleanly."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='assigner', email='assigner@example.com',
            password='testpass123', is_staff=False,
        )
        self.project = Project.objects.create(
            name='PLS-1339 Project', key='P1339', org_id=1,
            access_level='open',
        )
        ProjectMembership.objects.create(
            project=self.project, user_id=self.user.id,
            user_email=self.user.email, role='member',
            can_browse=True, can_create=True, can_edit=True, can_delete=False,
        )
        self.admin_patcher = patch(ADMIN_SERVICE_PATH)
        self.admin_patcher.start()
        self.user_svc_patcher = patch(USER_SERVICE_PATH)
        mock_user_svc = self.user_svc_patcher.start()
        mock_user_svc.resolve_user_info.return_value = None
        self.mock_user_svc = mock_user_svc

    def tearDown(self):
        self.admin_patcher.stop()
        self.user_svc_patcher.stop()

    def test_bare_assign_on_team_routed_item_retains_group(self):
        self.task = Item.objects.create(
            project=self.project, item_key='P1339-1', mode='task',
            title='carrier', status='todo',
            assignment_group='qa',
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            f'/api/items/{self.task.id}/assign/', {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Structured body, never silent-empty
        self.assertTrue(response.data)
        self.assertEqual(response.data.get('assignment_group'), 'qa')
        self.assertTrue(response.data.get('queued_group_only'))
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignment_group, 'qa')
        self.assertIsNone(self.task.assignee_id)
        self.assertIsNone(self.task.assignee_email)

    def test_bare_assign_on_owned_item_is_explicit_unassign(self):
        # PLAT-4572 contract preserved: bare POST on an item WITH a concrete
        # owner is the explicit unassign command — group nulls, exact receipt.
        self.task = Item.objects.create(
            project=self.project, item_key='P1339-4', mode='task',
            title='owned', status='todo', assignment_group='qa',
        )
        Item.objects.filter(pk=self.task.pk).update(
            assignee_email='zen@shizuha.com')
        self.task.refresh_from_db()
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            f'/api/items/{self.task.id}/assign/', {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data)
        self.assertIsNone(response.data.get('assignment_group'))
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assignment_group)
        self.assertIsNone(self.task.assignee_email)

    def test_bare_assign_on_ungrouped_item_unassigns(self):
        self.task = Item.objects.create(
            project=self.project, item_key='P1339-2', mode='task',
            title='plain', status='todo',
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            f'/api/items/{self.task.id}/assign/', {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data)
        self.assertIsNone(response.data.get('assignment_group'))
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assignment_group)
        self.assertIsNone(self.task.assignee_email)

    def test_explicit_foreign_group_still_rejected_structured(self):
        self.task = Item.objects.create(
            project=self.project, item_key='P1339-3', mode='task',
            title='routed', status='todo', assignment_group='qa',
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            f'/api/items/{self.task.id}/assign/',
            {'assignment_group': 'cortex-phantom'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(response.data)
        self.assertIn('error', response.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignment_group, 'qa')
