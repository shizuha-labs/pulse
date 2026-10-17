"""HIVE-1223: fresh-org first-task onboarding.

An org with zero projects could not create its first task — ``POST /api/items/``
returned 400 ``{"project": ["A task must belong to an organization project."]}``
because ``ItemCreateSerializer.validate`` hard-requires a project. Per the architect
contract (aoi, option 1) the BACKEND now guarantees the invariant: on a ``mode=task``
create with no ``project`` where the resolved org has zero projects, it lazily
``get_or_create``s the org's canonical default project (``is_default=True``, bound to
the RESOLVED org) and attaches the task — so EVERY caller (Hive CTA, raw API, MCP
agents) can create a first task, not just a UI that can click a project-creation flow.

Regressions hit the REAL routed view ``POST /api/items/`` in production order
(verify-before-push): the invariant is the caller SEQUENCE, so the idempotency test
issues two consecutive top-level first-task POSTs and asserts a single default.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from tasks.models import Item, Project, TeamRoster

User = get_user_model()

ITEMS_URL = '/api/items/'


class Hive1223OrgDefaultProjectTests(APITestCase):
    def setUp(self):
        # perform_create resolves users/teams via user_service + workflow_service,
        # which cannot reach shizuha-id in tests — stub them (mirrors test_assignment).
        self._patches = [
            mock.patch('tasks.user_service.user_service.get_user_by_email',
                       side_effect=self._by_email),
            mock.patch('tasks.user_service.user_service.get_user_by_id',
                       side_effect=self._by_id),
            mock.patch('tasks.services.workflow_service.WorkflowService._is_known_team',
                       return_value=True),
            mock.patch('tasks.services.workflow_service.WorkflowService.assignment_org_mismatch',
                       return_value=(False, None, None)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self._known = {}

    def _by_email(self, email):
        u = self._known.get((email or '').strip().lower())
        return {'id': u.id, 'email': u.email} if u else None

    def _by_id(self, uid):
        for u in self._known.values():
            if int(uid) == u.id:
                return {'id': u.id, 'email': u.email}
        return None

    def _user(self, username, org_id):
        u = User.objects.create_user(
            username=username, email=f'{username}@shizuha.com', password='Pass123!x',
        )
        u.organization_memberships = {org_id: 'member'}
        self._known[u.email.lower()] = u
        return u

    def _post_first_task(self, user, org_id, title):
        client = APIClient()
        client.force_authenticate(user=user)
        client.raise_request_exception = False
        return client.post(
            ITEMS_URL,
            {'title': title, 'mode': 'task', 'workflow_name': 'simple'},
            format='json',
            HTTP_X_ORGANIZATION_ID=str(org_id),
        )

    def test_fresh_org_first_task_provisions_default_project_in_same_org(self):
        org_id = 940015
        user = self._user('hive1223a', org_id)
        self.assertFalse(Project.objects.filter(org_id=org_id).exists())

        resp = self._post_first_task(user, org_id, 'first task')

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        proj = Project.objects.get(org_id=org_id, is_default=True)
        # bound to the RESOLVED org (tenant-isolation crux), org-scoped access
        self.assertEqual(proj.org_id, org_id)
        self.assertTrue(proj.is_default)
        self.assertEqual(proj.access_level, 'open')
        # the created task is attached to that default project
        item = Item.objects.get(title='first task')
        self.assertEqual(item.project_id, proj.id)

    def test_two_consecutive_first_tasks_converge_to_one_default(self):
        org_id = 940016
        user = self._user('hive1223b', org_id)

        r1 = self._post_first_task(user, org_id, 'task one')
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED, r1.content)
        r2 = self._post_first_task(user, org_id, 'task two')
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED, r2.content)

        # exactly ONE default (idempotent get_or_create + partial-unique backstop)
        self.assertEqual(
            Project.objects.filter(org_id=org_id, is_default=True).count(), 1,
        )
        proj = Project.objects.get(org_id=org_id, is_default=True)
        self.assertEqual(Item.objects.filter(project_id=proj.id).count(), 2)

    def test_default_project_bound_to_resolved_org_not_leaked_cross_org(self):
        org_a, org_b = 940017, 940018
        user_a = self._user('hive1223c', org_a)

        self._post_first_task(user_a, org_a, 'a-task')

        proj_a = Project.objects.get(org_id=org_a, is_default=True)
        self.assertEqual(proj_a.org_id, org_a)
        # nothing bled into org B
        self.assertFalse(Project.objects.filter(org_id=org_b).exists())

    def test_org_with_existing_project_does_not_autoprovision(self):
        # Hot-path guard: an org that already has projects is NOT "fresh" — a
        # no-project task-create still returns the original 400 (pick a project),
        # and no default is provisioned.
        org_id = 940019
        user = self._user('hive1223d', org_id)
        Project.objects.create(
            org_id=org_id, name='Existing', key='EXIST', access_level='open',
        )

        resp = self._post_first_task(user, org_id, 'task in populated org')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertFalse(
            Project.objects.filter(org_id=org_id, is_default=True).exists(),
        )

    def _post_on_project(self, user, org_id, project, title):
        client = APIClient()
        client.force_authenticate(user=user)
        client.raise_request_exception = False
        return client.post(
            ITEMS_URL,
            {'title': title, 'mode': 'task', 'project': project.id},
            format='json',
            HTTP_X_ORGANIZATION_ID=str(org_id),
        )

    def test_project_without_assignee_self_assigns_when_org_has_no_engineering_roster(self):
        # Production order: POST /api/items/ with a project and no assignee.
        # perform_create used to stamp assignment_group='engineering' whenever
        # _derive_owner_team returned None. On a customer org that has no local
        # engineering roster, that borrowed the platform roster, capacity-queued
        # the task, and left it with no owner. The creator owns it instead.
        org_id = 940020
        user = self._user('hive1223e', org_id)
        project = Project.objects.create(
            org_id=org_id, name='Work', key='WORK', access_level='open', is_default=True,
        )

        resp = self._post_on_project(user, org_id, project, 'owned by creator')

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        item = Item.objects.get(title='owned by creator')
        self.assertEqual(item.project_id, project.id)
        self.assertEqual(item.assignee_id, user.id)
        self.assertEqual((item.assignee_email or '').lower(), user.email.lower())
        self.assertFalse(item.assignment_group)

    def test_project_without_assignee_keeps_engineering_when_org_roster_exists(self):
        org_id = 940021
        user = self._user('hive1223f', org_id)
        project = Project.objects.create(
            org_id=org_id, name='Work', key='WORK', access_level='open', is_default=True,
        )
        TeamRoster.objects.create(
            team_name='engineering',
            organization_slug='local-940021',
            organization_id=org_id,
            tiers=[[user.email]],
            is_active=True,
        )

        resp = self._post_on_project(user, org_id, project, 'team routed')

        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        item = Item.objects.get(title='team routed')
        self.assertEqual(item.assignment_group, 'engineering')
