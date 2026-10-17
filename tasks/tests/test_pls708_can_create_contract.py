"""PLS-708 regression: project-list can_create must match create-item authorization.

Focused live QA (PLAT-4838, mika@shizuha.com, non-staff) found `/pulse/api/projects/`
reporting `can_create=true` on open projects (335 Pulse, 331 Platform) while every
valid owning-bug create attempt was rejected with HTTP 400
`{'project':'You do not have permission to create items in this project'}` — a
contradictory capability contract that forced an ANDON.

Root cause: `ProjectSerializer.get_can_create` returned True for every
`access_level == 'open'` project WITHOUT verifying org membership, and for
membership-based projects ignored the org-membership gate that the create path
(`PermissionService.can_access_project(permission='create')`) enforces first.

Fix: `get_can_create` now calls the EXACT same authorization the create path
uses — one source of truth. `can_create=true` ⟺ the same principal can create a
valid item in that exact project.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from tasks.models import Item, Project, ProjectMembership
from tasks.serializers import ProjectSerializer
from tasks.services.permissions import PermissionService

User = get_user_model()


class ProjectCanCreateMatchesCreateAuthorizationTests(APITestCase):
    """can_create=true must imply the same principal can create in that project."""

    def setUp(self):
        self.client = APIClient()
        self.project = Project.objects.create(
            name='Pulse',
            key='PLS',
            org_id=1,
            access_level='open',
        )
        self.qa_user = User.objects.create_user(
            username='mika',
            email='mika@shizuha.com',
            password='testpass123',
            is_staff=False,
        )

    def _serializer_can_create(self, user):
        """Compute get_can_create directly (bypasses list visibility, which is
        intentionally broader and would hide the project from a non-org-member)."""
        from rest_framework.test import APIRequestFactory
        factory = APIRequestFactory()
        request = factory.get('/api/projects/')
        request.user = user
        ser = ProjectSerializer(self.project, context={'request': request})
        return ser.data['can_create']

    def _create_item(self, title):
        return self.client.post(
            '/api/items/',
            {
                'title': title,
                'mode': 'task',
                'project': self.project.id,
                'workflow_name': 'simple',
            },
            format='json',
        )

    def test_non_org_member_open_project_can_create_false(self):
        """The reported PLS-708 split: a principal who is NOT an org member of the
        project's org (so create-item is denied) must see can_create=false, not
        true — even for an open project. This is the regression the old
        unconditional `access_level == 'open' -> True` produced."""
        # Mika has a project membership with can_create=True but is NOT an org
        # member of org 1 — the create-item authorization denies her.
        self.qa_user.organization_memberships = {}
        ProjectMembership.objects.create(
            project=self.project,
            user_email='mika@shizuha.com',
            role='member',
            can_create=True,
        )
        self.client.force_authenticate(user=self.qa_user)

        # Create-item is denied (not an org member of the project's org).
        create_resp = self._create_item('QA smoke task')
        self.assertEqual(
            create_resp.status_code, status.HTTP_400_BAD_REQUEST,
            create_resp.content[:200],
        )
        # The capability bit must agree with the create authorization.
        self.assertFalse(self._serializer_can_create(self.qa_user))

    def test_org_member_open_project_can_create_true(self):
        """An org member of an open project sees can_create=true and can create."""
        self.qa_user.organization_memberships = {1: 'member'}
        self.client.force_authenticate(user=self.qa_user)
        self.assertTrue(self._serializer_can_create(self.qa_user))
        resp = self._create_item('QA smoke task 2')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content[:200])

    def test_org_admin_can_create_true(self):
        """An org owner/admin of an open project sees can_create=true."""
        self.qa_user.organization_memberships = {1: 'admin'}
        self.client.force_authenticate(user=self.qa_user)
        self.assertTrue(self._serializer_can_create(self.qa_user))

    def test_can_create_matches_service_for_all_roles(self):
        """Property check: get_can_create == can_access_project(permission='create')
        for a matrix of org roles and memberships — one source of truth."""
        cases = [
            ({}, None),                    # no org, no membership -> denied
            ({}, True),                    # no org, membership can_create -> denied (org gate)
            ({1: 'member'}, None),         # org member, no membership -> open project allows
            ({1: 'member'}, True),         # org member + membership -> allowed
            ({1: 'admin'}, None),          # org admin -> allowed
            ({1: 'owner'}, None),          # org owner -> allowed
        ]
        for idx, (org_claim, pm_can_create) in enumerate(cases):
            u = User.objects.create_user(
                username='u%d' % idx,
                email='u%d@example.com' % idx,
                password='x',
                is_staff=False,
            )
            u.organization_memberships = org_claim
            if pm_can_create is not None:
                ProjectMembership.objects.create(
                    project=self.project,
                    user_email='u%d@example.com' % idx,
                    role='member',
                    can_create=pm_can_create,
                )
            expected = PermissionService.can_access_project(
                u.id, self.project, permission='create',
                user_email=u.email, user=u,
            )
            self.assertEqual(
                self._serializer_can_create(u), expected,
                'can_create mismatch for org_claim=%r pm_can_create=%r' % (
                    org_claim, pm_can_create,
                ),
            )

    def test_cross_org_project_can_create_false(self):
        """A principal who is an org member of org 2 but not org 1 cannot create in
        the org-1 project — can_create must be false."""
        self.qa_user.organization_memberships = {2: 'member'}
        self.client.force_authenticate(user=self.qa_user)
        self.assertFalse(self._serializer_can_create(self.qa_user))

    def test_archived_project_can_create_false(self):
        """An archived project must not advertise can_create=true."""
        self.qa_user.organization_memberships = {1: 'member'}
        self.project.is_archived = True
        self.project.save(update_fields=['is_archived'])
        self.client.force_authenticate(user=self.qa_user)
        self.assertFalse(self._serializer_can_create(self.qa_user))
