"""
PLAT-257 regression tests — task PARTICIPANTS (assignee/reporter) must be able
to comment (POST) and write fields (PATCH labels) on:
  (a) project-less items, and
  (b) items in a project they are NOT a member of,
matched by canonical email/alias, with the legacy numeric ID used only when the
corresponding stored email is blank.

Before the fix, CommentViewSet._can_access_item only matched project-less items
by user.id (no email path) and did no participant check at all for project
items, so assignees/reporters were denied comments; the item write path is
guarded by ItemProjectPermission, which now shares the same canonical
`is_item_participant` check.

These positive cases intentionally exercise the participant short-circuit, which
runs BEFORE any org/project membership lookup — so they make no admin-service
network calls. Project-owned fixtures mock only the exact organization-membership
authorization used when establishing the assignment; they deliberately create no
ProjectMembership, so the subsequent permission check still proves participant
access rather than project access. Negative controls use project-less items.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient

from tasks.models import Item, Project, ProjectMembership
from tasks.services.admin_service import admin_service
from tasks.services.permissions import PermissionService

User = get_user_model()


class PLAT257ParticipantCommentTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        # Non-staff participant (NOT a project member, NOT an org admin).
        self.user = User.objects.create_user(
            username='ryo', email='ryo@shizuha.com', password='x', is_staff=False,
        )
        # A different non-participant, non-staff user (negative control).
        self.other = User.objects.create_user(
            username='intruder', email='intruder@shizuha.com', password='x', is_staff=False,
        )
        # Project the participant is NOT a member of.
        self.project = Project.objects.create(
            name='Evolution', key='EVOL', org_id=1, access_level='private', is_active=True,
        )
        self.org_member = patch.object(
            admin_service, 'is_org_member', return_value=True,
        )
        self.org_members = patch.object(
            admin_service, 'get_org_members',
            return_value=[{'user_email': self.user.email}],
        )
        self.org_member.start()
        self.org_members.start()
        self.addCleanup(self.org_member.stop)
        self.addCleanup(self.org_members.stop)

    def _post_comment(self, item):
        return self.client.post('/api/comments/', {'item': item.id, 'content': 'hi'}, format='json')

    def test_assignee_by_id_can_comment_on_projectless_item(self):
        item = Item.objects.create(mode='task', title='T', assignee_id=self.user.id)
        self.client.force_authenticate(self.user)
        self.assertEqual(self._post_comment(item).status_code, 201)

    def test_reporter_by_email_can_comment_on_projectless_item(self):
        # created_by_id intentionally unset/mismatched → must match by email.
        item = Item.objects.create(
            mode='task', title='T', created_by_id=None, created_by_email='ryo@shizuha.com',
        )
        self.client.force_authenticate(self.user)
        self.assertEqual(self._post_comment(item).status_code, 201)

    def test_assignee_can_comment_on_item_in_nonmember_project(self):
        item = Item.objects.create(
            mode='task', title='T', project=self.project,
            assignee_id=self.user.id, assignee_email=self.user.email,
        )
        self.client.force_authenticate(self.user)
        self.assertEqual(self._post_comment(item).status_code, 201)

    def test_reporter_by_email_can_comment_on_item_in_nonmember_project(self):
        item = Item.objects.create(
            mode='task', title='T', project=self.project,
            created_by_id=None, created_by_email='ryo@shizuha.com',
        )
        self.client.force_authenticate(self.user)
        self.assertEqual(self._post_comment(item).status_code, 201)

    def test_participant_email_match_is_case_insensitive(self):
        # Stored assignee_email differs only in case from the token email.
        item = Item.objects.create(
            mode='task', title='T', project=self.project,
            assignee_id=None, assignee_email='RYO@Shizuha.COM',
        )
        self.client.force_authenticate(self.user)  # user.email == 'ryo@shizuha.com'
        self.assertEqual(self._post_comment(item).status_code, 201)

    def test_participant_email_match_expands_agent_alias(self):
        # Stored assignee_email uses the agent-domain alias while the token/user
        # identity is the company-domain email. This must still be the same
        # participant and must bypass project membership gates.
        item = Item.objects.create(
            mode='task', title='T', project=self.project,
            assignee_id=None, assignee_email='ryo@agents.shizuha.io',
        )
        self.client.force_authenticate(self.user)  # user.email == 'ryo@shizuha.com'
        self.assertEqual(self._post_comment(item).status_code, 201)

    def test_reporter_email_match_expands_agent_alias(self):
        item = Item.objects.create(
            mode='task', title='T', project=self.project,
            created_by_id=None, created_by_email='ryo@agents.shizuha.io',
        )
        self.client.force_authenticate(self.user)
        self.assertEqual(self._post_comment(item).status_code, 201)

    def test_non_participant_still_denied_on_projectless_item(self):
        item = Item.objects.create(mode='task', title='T', assignee_id=self.user.id)
        self.client.force_authenticate(self.other)
        # Denied with a 400 ValidationError from perform_create.
        self.assertEqual(self._post_comment(item).status_code, 400)

    def test_colliding_assignee_id_cannot_override_nonblank_email(self):
        item = Item.objects.create(
            mode='task', title='T', project=self.project,
            assignee_id=self.user.id,
            assignee_email='canonical-other@example.com',
        )
        self.client.force_authenticate(self.user)
        self.assertEqual(self._post_comment(item).status_code, 400)

    def test_colliding_reporter_id_cannot_override_nonblank_email(self):
        item = Item.objects.create(
            mode='task', title='T', project=self.project,
            created_by_id=self.user.id,
            created_by_email='canonical-other@example.com',
        )
        self.client.force_authenticate(self.user)
        self.assertEqual(self._post_comment(item).status_code, 400)


class PLAT257ParticipantPatchTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='ryo', email='ryo@shizuha.com', password='x', is_staff=False,
        )
        self.other = User.objects.create_user(
            username='intruder', email='intruder@shizuha.com', password='x', is_staff=False,
        )
        self.project = Project.objects.create(
            name='Evolution', key='EVOL', org_id=1, access_level='private', is_active=True,
        )
        self.org_member = patch.object(
            admin_service, 'is_org_member', return_value=True,
        )
        self.org_members = patch.object(
            admin_service, 'get_org_members',
            return_value=[{'user_email': self.user.email}],
        )
        self.org_member.start()
        self.org_members.start()
        self.addCleanup(self.org_member.stop)
        self.addCleanup(self.org_members.stop)

    def _patch_labels(self, item):
        return self.client.patch(f'/api/items/{item.id}/', {'labels': ['no-pr']}, format='json')

    def test_assignee_can_patch_labels_in_nonmember_project(self):
        item = Item.objects.create(
            mode='task', title='T', project=self.project,
            assignee_id=self.user.id, assignee_email=self.user.email,
        )
        self.client.force_authenticate(self.user)
        self.assertEqual(self._patch_labels(item).status_code, 200)

    def test_reporter_by_email_can_patch_labels_on_projectless_item(self):
        item = Item.objects.create(
            mode='task', title='T', created_by_id=None, created_by_email='ryo@shizuha.com',
        )
        self.client.force_authenticate(self.user)
        self.assertEqual(self._patch_labels(item).status_code, 200)

    def test_assignee_agent_alias_can_patch_labels_in_nonmember_project(self):
        item = Item.objects.create(
            mode='task', title='T', project=self.project,
            assignee_id=None, assignee_email='ryo@agents.shizuha.io',
        )
        self.client.force_authenticate(self.user)
        self.assertEqual(self._patch_labels(item).status_code, 200)

    def test_non_participant_denied_patch_on_projectless_item(self):
        item = Item.objects.create(mode='task', title='T', assignee_id=self.user.id)
        self.client.force_authenticate(self.other)
        # ItemProjectPermission denies → 403 (or 404 if filtered); must NOT be 200.
        self.assertIn(self._patch_labels(item).status_code, (403, 404))

    def test_colliding_id_cannot_patch_when_canonical_email_differs(self):
        item = Item.objects.create(
            mode='task', title='T', project=self.project,
            assignee_id=self.user.id,
            assignee_email='canonical-other@example.com',
        )
        self.client.force_authenticate(self.user)
        self.assertIn(self._patch_labels(item).status_code, (403, 404))

    def test_colliding_id_cannot_bypass_viewer_edit_permissions(self):
        ProjectMembership.objects.create(
            project=self.project,
            user_id=self.user.id,
            user_email=self.user.email,
            role='viewer',
        )
        item = Item.objects.create(
            mode='task', title='T', project=self.project,
            assignee_id=self.user.id,
            assignee_email='canonical-other@example.com',
        )
        self.client.force_authenticate(self.user)
        self.assertEqual(self._patch_labels(item).status_code, 403)


class PLS415PermissionServiceParticipantScopingTests(APITestCase):
    """List/queryset and object checks share the email-primary invariant."""

    def setUp(self):
        self.client = APIClient()
        self.legit = User.objects.create_user(
            username='legit', email='legit@org1.test', password='x', is_staff=False,
        )
        self.caller = User.objects.create_user(
            username='org2', email='org2@shizuha.com', password='x', is_staff=False,
        )
        self.caller.organization_memberships = {1: 'member', 2: 'member'}
        self.project = Project.objects.create(
            name='Private org1', key='PATT', org_id=1,
            access_level='private', is_active=True,
        )
        self.org_member = patch.object(
            admin_service, 'is_org_member', return_value=True,
        )
        self.org_member.start()
        self.addCleanup(self.org_member.stop)

    def _create_private_item(self, item_key='PATT-1', **overrides):
        values = {
            'mode': 'task',
            'title': 'Private task',
            'item_key': item_key,
            'project': self.project,
            'assignee_id': self.legit.id,
            'assignee_email': self.legit.email,
            'created_by_id': self.legit.id,
            'created_by_email': self.legit.email,
        }
        values.update(overrides)
        return Item.objects.create(**values)

    def _listed_keys(self):
        self.client.force_authenticate(self.caller)
        response = self.client.get('/api/items/', {'mode': 'task'})
        self.assertEqual(response.status_code, 200)
        return {row['item_key'] for row in response.data['results']}

    def test_item_list_denies_colliding_assignee_id_with_nonblank_email(self):
        self._create_private_item(
            assignee_id=self.caller.id,
            assignee_email=self.legit.email,
        )
        self.assertNotIn('PATT-1', self._listed_keys())

    def test_item_list_denies_colliding_reporter_id_with_nonblank_email(self):
        self._create_private_item(
            created_by_id=self.caller.id,
            created_by_email=self.legit.email,
        )
        self.assertNotIn('PATT-1', self._listed_keys())

    def test_service_object_and_queryset_deny_colliding_id(self):
        item = self._create_private_item(
            assignee_id=self.caller.id,
            assignee_email=self.legit.email,
        )
        queryset = PermissionService.get_user_item_queryset(user=self.caller)
        self.assertFalse(queryset.filter(pk=item.pk).exists())
        self.assertFalse(PermissionService.can_access_item(
            self.caller.id,
            item,
            permission='browse',
            user_email=self.caller.email,
        ))

    def test_alias_and_per_slot_blank_email_fallback_are_positive(self):
        alias = self._create_private_item(
            item_key='PATT-1',
            assignee_id=self.legit.id,
            assignee_email='org2@agents.shizuha.io',
        )
        # New project assignments require canonical emails at the DB boundary;
        # the fallback remains necessary for legacy/projectless rows.
        legacy_assignee = Item.objects.create(
            mode='task', title='Legacy assignee', item_key='PATT-2',
            assignee_id=self.caller.id,
            assignee_email='',
        )
        legacy_reporter = Item.objects.create(
            mode='task', title='Legacy reporter', item_key='PATT-3',
            created_by_id=self.caller.id,
            created_by_email='',
        )

        listed = self._listed_keys()
        self.assertTrue({'PATT-1', 'PATT-2', 'PATT-3'} <= listed)
        for item in (alias, legacy_assignee, legacy_reporter):
            self.assertTrue(PermissionService.can_access_item(
                self.caller.id,
                item,
                permission='browse',
                user_email=self.caller.email,
            ))
