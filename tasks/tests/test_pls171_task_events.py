"""PLS-171 — TaskEvent delta log (Slice 1): signal emission, cursor feed, access scoping."""
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from tasks.models import Project, Item, Comment, ItemLink, TaskEvent
from tasks.services.workflow_service import WorkflowService
from tasks.views import TaskEventListView


class _U:
    """Lightweight authenticated-user stand-in (the view only reads id/is_superuser)."""
    def __init__(self, uid, is_superuser=False, email='u@example.com'):
        self.id = uid
        self.pk = uid
        self.is_superuser = is_superuser
        self.is_staff = is_superuser
        self.email = email
        self.is_authenticated = True


class PLS171SignalEmissionTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='TST', org_id=7, name='Test Project')

    def _kinds_for(self, item_pk):
        return [e.kind for e in TaskEvent.objects.filter(item_id=item_pk).order_by('seq')]

    def test_item_create_update_delete_emit_events(self):
        item = Item.objects.create(project=self.project, title='hello', status='open')
        self.assertIn(TaskEvent.KIND_ITEM_CREATED, self._kinds_for(item.pk))

        item.refresh_from_db()
        ev = TaskEvent.objects.filter(item_id=item.pk, kind=TaskEvent.KIND_ITEM_CREATED).first()
        self.assertEqual(ev.project_id, self.project.pk)
        self.assertEqual(ev.org_id, 7)
        # the delta carries the item's actual saved status (the model may normalize it)
        self.assertEqual(ev.payload.get('status'), item.status)

        item.status = 'done'
        item.save()
        item.refresh_from_db()
        self.assertIn(TaskEvent.KIND_ITEM_UPDATED, self._kinds_for(item.pk))
        upd = TaskEvent.objects.filter(item_id=item.pk, kind=TaskEvent.KIND_ITEM_UPDATED).last()
        self.assertEqual(upd.payload.get('status'), item.status)

        pk = item.pk
        item.delete()
        self.assertIn(TaskEvent.KIND_ITEM_DELETED, self._kinds_for(pk))

    def test_comment_emits_event(self):
        item = Item.objects.create(project=self.project, title='c', status='open')
        Comment.objects.create(item=item, author_id=42, author_email='a@example.com', content='hi')
        kinds = self._kinds_for(item.pk)
        self.assertIn(TaskEvent.KIND_COMMENT_ADDED, kinds)

    def test_link_create_and_delete_emit_events(self):
        a = Item.objects.create(project=self.project, title='a', status='open')
        b = Item.objects.create(project=self.project, title='b', status='open')
        link = ItemLink.objects.create(source_item=a, target_item=b, link_type='blocks', created_by_id=1)
        ev = TaskEvent.objects.filter(item_id=a.pk, kind=TaskEvent.KIND_LINK_CHANGED).first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.payload.get('link_type'), 'blocks')
        link.delete()
        # a second link_changed (the removal) is appended for the source item
        self.assertGreaterEqual(
            TaskEvent.objects.filter(item_id=a.pk, kind=TaskEvent.KIND_LINK_CHANGED).count(), 2)


class PLS171EventsEndpointTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.project = Project.objects.create(key='EVT', org_id=7, name='Evt Project')
        assignment_authorization = mock.patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'fixture-org', 'fixture-org'),
        )
        assignment_authorization.start()
        self.addCleanup(assignment_authorization.stop)

    def _get(self, user, **params):
        req = self.factory.get('/api/events/', params)
        force_authenticate(req, user=user)
        return TaskEventListView.as_view()(req)

    def test_superuser_sees_events_and_cursor_advances(self):
        i = Item.objects.create(project=self.project, title='x', status='open')
        resp = self._get(_U(1, is_superuser=True))
        self.assertEqual(resp.status_code, 200)
        self.assertGreaterEqual(len(resp.data['events']), 1)
        cursor = resp.data['cursor']
        self.assertGreater(cursor, 0)

        # since=<cursor> returns nothing new until another mutation
        self.assertEqual(self._get(_U(1, is_superuser=True), since=cursor).data['events'], [])
        i.status = 'done'; i.save()
        nxt = self._get(_U(1, is_superuser=True), since=cursor)
        self.assertGreaterEqual(len(nxt.data['events']), 1)
        self.assertGreater(nxt.data['cursor'], cursor)

    def test_since_and_limit_window(self):
        for n in range(5):
            Item.objects.create(project=self.project, title=f't{n}', status='open')
        resp = self._get(_U(1, is_superuser=True), since=0, limit=2)
        self.assertEqual(len(resp.data['events']), 2)
        self.assertTrue(resp.data['has_more'])
        # events are oldest-first by seq
        seqs = [e['seq'] for e in resp.data['events']]
        self.assertEqual(seqs, sorted(seqs))

    def test_non_member_is_access_scoped_out(self):
        # A non-superuser with no project membership must see NO project events.
        Item.objects.create(project=self.project, title='secret', status='open')
        resp = self._get(_U(99999, is_superuser=False, email='nobody@example.com'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['events'], [])

    def test_l3_assignee_sees_their_item_in_a_non_member_project(self):
        # L3 owner-visibility: a user who is the ASSIGNEE of an item still gets its
        # live deltas even with no membership in that item's project.
        mine = Item.objects.create(project=self.project, title='assigned-to-me',
                                   status='open', assignee_email='owner@example.com')
        Item.objects.create(project=self.project, title='not-mine', status='open')
        resp = self._get(_U(777, is_superuser=False, email='owner@example.com'))
        self.assertEqual(resp.status_code, 200)
        item_ids = {e['item_id'] for e in resp.data['events']}
        self.assertIn(mine.pk, item_ids)          # sees their assigned item
        # and does NOT see the other (non-member, not theirs) item
        self.assertEqual(item_ids, {mine.pk})

    def test_l3_alias_email_match(self):
        # PLAT-533: assignee written as the @agents.shizuha.io alias, caller authed
        # as @shizuha.com — must still match.
        mine = Item.objects.create(project=self.project, title='alias', status='open',
                                   assignee_email='kai@agents.shizuha.io')
        resp = self._get(_U(778, is_superuser=False, email='kai@shizuha.com'))
        item_ids = {e['item_id'] for e in resp.data['events']}
        self.assertIn(mine.pk, item_ids)
