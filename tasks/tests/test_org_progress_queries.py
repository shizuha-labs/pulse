"""Real PostgreSQL caller controls for the Home org-progress read path."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tasks.models import Item, ItemActivity, Project
from tasks.services import stage_metrics as metrics
from tasks.views import ItemViewSet


class OrgProgressQueryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(name='Progress scoped', key='PRQ', org_id=1)
        cls.foreign = Project.objects.create(name='Foreign progress', key='FRQ', org_id=2)

    def setUp(self):
        self.now = timezone.now()

    def item(self, *, project=None, status='in_progress', age=40, mode='task'):
        item = Item.objects.create(project=project or self.project, title='metric fixture',
                                   status=status, assignment_group='engineering', mode=mode)
        Item.objects.filter(pk=item.pk).update(created_at=self.now - timedelta(days=age))
        item.refresh_from_db()
        # Creation signals are independent of the explicit history under test.
        ItemActivity.objects.filter(item=item).delete()
        return item

    def transition(self, item, old, new, days):
        event = ItemActivity.objects.create(item=item, field_name=metrics.STATUS_FIELD_LABEL,
                                           action='status_changed', old_value=old,
                                           new_value=new, user_id=1)
        ItemActivity.objects.filter(pk=event.pk).update(created_at=self.now - timedelta(days=days))
        return event

    def scoped(self):
        return Item.objects.filter(project=self.project)

    def test_query_count_is_constant_for_many_items(self):
        for _ in range(16):
            item = self.item()
            self.transition(item, 'open', 'in_progress', 2)
        with self.assertNumQueries(2):
            closed = metrics.team_throughput(items=self.scoped(), days=7, now=self.now)
        with self.assertNumQueries(2):
            current = metrics.current_stage_dwell(items=self.scoped(), now=self.now)
        self.assertEqual(closed[0]['samples'], 16)
        self.assertEqual(current[0]['samples'], 16)

    def test_window_exit_preserves_old_carry_in_and_prior_status(self):
        item = self.item(status='in_review')
        self.transition(item, 'open', 'in_progress', 30)
        # The preceding new_value is authoritative even for a malformed old label.
        self.transition(item, 'incorrect-old-label', 'in_review', 2)
        rows = metrics.team_throughput(items=self.scoped(), days=7, now=self.now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'in_progress')
        self.assertEqual(rows[0]['avg_dwell_seconds'], 28 * 86400)
        self.assertEqual(rows[0]['throughput_out_per_day'], round(1 / 7, 2))

    def test_first_exit_starts_at_creation_and_latest_open_entry(self):
        item = self.item(age=10)
        self.transition(item, 'open', 'in_progress', 2)
        closed = metrics.team_throughput(items=self.scoped(), days=7, now=self.now)
        current = metrics.current_stage_dwell(items=self.scoped(), now=self.now)
        self.assertEqual(closed[0]['avg_dwell_seconds'], 8 * 86400)
        self.assertEqual(current[0]['avg_dwell_seconds'], 2 * 86400)

    def test_no_history_parking_terminal_alert_and_foreign_scope(self):
        self.item(age=3)
        self.item(status='scheduled')
        self.item(status='done')
        self.item(mode='alert')
        self.item(project=self.foreign, age=89)
        current = metrics.current_stage_dwell(items=self.scoped(), now=self.now)
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['samples'], 1)
        self.assertEqual(current[0]['avg_dwell_seconds'], 3 * 86400)
        parked = metrics.current_stage_dwell(items=self.scoped(), now=self.now, include_parking=True)
        self.assertEqual({row['status'] for row in parked}, {'scheduled', 'in_progress'})

    def test_actual_org_progress_caller_preserves_scope_and_bounds_queries(self):
        for _ in range(12):
            item = self.item()
            self.transition(item, 'open', 'in_progress', 2)
        foreign = self.item(project=self.foreign)
        self.transition(foreign, 'foreign', 'in_progress', 1)
        request = SimpleNamespace(query_params={'hours': '24', 'buckets': '24', 'days': '7'})
        view = ItemViewSet()
        # The production authz queryset is the input contract; this test does
        # not replace it with an unscoped manager in the implementation.
        with patch.object(view, 'get_queryset', return_value=self.scoped().order_by('-created_at')):
            with CaptureQueriesContext(connection) as queries:
                response = view.org_progress(request)
        self.assertLessEqual(len(queries), 9)
        self.assertEqual(response.data['by_status'], {'in_progress': 12})
        self.assertEqual(response.data['snapshot']['open'], 12)
        self.assertEqual(response.data['open_dwell'][0]['samples'], 12)
        self.assertNotIn('foreign', {r['status'] for r in response.data['throughput']})

    def test_many_ancient_events_do_not_hydrate_old_history(self):
        item = self.item(age=90)
        for age in range(80, 20, -1):
            self.transition(item, 'open', 'in_progress', age)
        self.transition(item, 'in_progress', 'in_review', 1)
        with CaptureQueriesContext(connection) as queries:
            rows = metrics.team_throughput(items=self.scoped(), days=7, now=self.now)
        self.assertEqual(len(queries), 2)
        self.assertEqual(rows[0]['samples'], 1)
        self.assertEqual(rows[0]['avg_dwell_seconds'], 20 * 86400)
        self.assertIn('DISTINCT ON', queries[1]['sql'])

    def test_actual_status_projection_counts_all_rows_and_preserves_category(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from tasks.models import Status
        self.project.access_level = 'open'
        self.project.save(update_fields=['access_level'])
        Status.objects.update_or_create(slug='building', defaults={'name': 'Building', 'category': 'in_progress'})
        Item.objects.bulk_create([
            Item(project=self.project, title='visible count', status='building', mode='task', workflow_name='simple')
            for _ in range(73)
        ])
        Item.objects.bulk_create([
            Item(project=self.foreign, title='foreign count', status='building', mode='task', workflow_name='simple'),
            Item(project=self.project, title='alert count', status='building', mode='alert'),
            Item(project=self.project, title='closed count', status='completed', mode='task', workflow_name='simple'),
        ])
        user = SimpleNamespace(id=7701, email='metrics@example.invalid', is_authenticated=True,
                               is_staff=False, is_superuser=False, organization_memberships={1: 'member'})
        request = APIRequestFactory().get('/api/items/statistics/', {
            'projection': 'status_counts', 'organization': 1, 'mode': 'task', 'is_active': 'true',
        })
        force_authenticate(request, user=user)
        with CaptureQueriesContext(connection) as queries:
            response = ItemViewSet.as_view({'get': 'statistics'})(request)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['total'], 73)
        self.assertEqual(response.data['counts'], [{'status': 'building', 'count': 73,
                                                   'status_category': 'in_progress'}])
        self.assertLessEqual(len(queries), 8)
        self.assertTrue(any('GROUP BY' in row['sql'] for row in queries))
        self.assertFalse(any('LIMIT 50' in row['sql'] for row in queries))

    def test_status_projection_rejects_foreign_org_and_anonymous_before_counts(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        user = SimpleNamespace(id=7701, email='metrics@example.invalid', is_authenticated=True,
                               is_staff=False, is_superuser=False, organization_memberships={1: 'member'})
        view = ItemViewSet.as_view({'get': 'statistics'})
        request = APIRequestFactory().get('/api/items/statistics/', {
            'projection': 'status_counts', 'organization': 2,
        })
        force_authenticate(request, user=user)
        with CaptureQueriesContext(connection) as queries:
            response = view(request)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(queries), 0)
        anonymous = view(APIRequestFactory().get('/api/items/statistics/?projection=status_counts'))
        self.assertIn(anonymous.status_code, (401, 403))


    def test_grouped_outputs_deduplicate_real_membership_join_rows(self):
        from tasks.models import ProjectMembership
        ProjectMembership.objects.bulk_create([
            ProjectMembership(project=self.project, user_id=881, can_browse=True),
            ProjectMembership(project=self.project, user_id=882, can_browse=True),
        ])
        for _ in range(4):
            self.item(status='in_progress')
        # The caller queryset can contain multiple grant-related joins even
        # with queryset DISTINCT. DISTINCT aggregate output cannot dedupe its
        # COUNT input; retain item identity inside the aggregate.
        scoped = self.scoped().filter(project__memberships__can_browse=True).distinct().order_by('-created_at')
        request = SimpleNamespace(query_params={'projection': 'status_counts'})
        view = ItemViewSet()
        with patch.object(view, 'get_queryset', return_value=scoped):
            counts = view.statistics(request).data
            progress = view.org_progress(SimpleNamespace(query_params={})).data
        self.assertEqual(counts['total'], 4)
        self.assertEqual(counts['counts'][0]['count'], 4)
        self.assertEqual(progress['by_status'], {'in_progress': 4})
        self.assertEqual(progress['snapshot']['open'], 4)


    def test_real_migrated_status_window_index_matches_query_predicate(self):
        import importlib
        from django.contrib.postgres.operations import AddIndexConcurrently
        migration = importlib.import_module('tasks.migrations.0187_item_status_window_index').Migration
        self.assertFalse(migration.atomic)
        self.assertIsInstance(migration.operations[0], AddIndexConcurrently)
        with connection.cursor() as cursor:
            cursor.execute("""SELECT i.indisvalid, i.indisready,
                pg_get_indexdef(i.indexrelid), pg_get_expr(i.indpred,i.indrelid)
                FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
                WHERE c.relname='item_status_time_item_idx'""")
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[:2], (True, True))
        self.assertIn('(created_at, item_id, id)', row[2])
        self.assertIn('field_name', row[3])
        self.assertIn("'Status'", row[3])
