import sys
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from tasks.models import Item, Project, Status, TeamRoster, Workflow, WorkflowStatus
from tasks.services.actionable_items import (
    annotate_owner_queue_projection,
    filter_runnable_owner_queue_items,
)


class RecurringInitialOwnerReadinessTests(APITestCase):
    def setUp(self):
        self.agent = get_user_model().objects.create_user(
            username='recurring-owner', email='recurring-owner@shizuha.com',
            password='x',
        )
        self.client.force_authenticate(self.agent)
        self.now = datetime(2026, 9, 13, 23, 10, tzinfo=datetime_timezone.utc)
        self.clock = mock.patch('django.utils.timezone.now', return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def item(self, title, **fields):
        values = {
            'mode': 'task', 'status': 'todo', 'workflow_name': 'reviewed-task',
            'assignee_id': self.agent.pk, 'assignee_email': self.agent.email,
            'is_recurring': True, 'frequency': 'daily',
            # Wall-clock immunity (kei's ANDON, 09-16): the due must be
            # forward-looking relative to the REAL wall clock, not the mocked
            # clock — `self.now + 2d` bakes a fixed datetime (2026-09-15
            # 23:10 UTC = 2026-09-16 04:40 IST) into the test source, and the
            # real clock advancing past it buckets the overdue item ready
            # (a standing ~19-hour daily merge-outage on the pulse lane).
            # The real clock + 2d is always ahead of every code path's clock.
            'due_date': datetime.now(datetime_timezone.utc) + timedelta(days=2),
            'priority': 'urgent',
        }
        values.update(fields)
        item = Item.objects.create(title=title, **values)
        Item.objects.filter(pk=item.pk).update(**values)
        item.refresh_from_db()
        return item

    def preflight(self):
        response = self.client.get('/api/items/heartbeat-preflight/', {
            'assignee_email': self.agent.email,
        })
        self.assertEqual(response.status_code, 200, response.content)
        return response.data

    def assert_waiting(self, item, waiting):
        projection = annotate_owner_queue_projection(
            Item.objects.filter(pk=item.pk),
        ).get()
        self.assertEqual(projection._queue_is_waiting, waiting)
        self.assertEqual(
            filter_runnable_owner_queue_items(Item.objects.filter(pk=item.pk)).exists(),
            not waiting,
        )

    def custom_workflow(self, slug, *, category='todo'):
        initial = Status.objects.create(
            slug=f'{slug}_initial', name=f'{slug} Initial', category=category,
        )
        workflow = Workflow.objects.create(
            slug=slug, name=f'{slug} Workflow', initial_status=initial,
        )
        WorkflowStatus.objects.create(workflow=workflow, status=initial)
        return workflow

    def test_live_reviewed_task_future_rows_do_not_wake_preflight(self):
        workflow = Workflow.objects.get(slug='reviewed-task')
        self.assertEqual(workflow.initial_status.slug, 'todo')
        self.assertEqual(workflow.initial_status.category, 'todo')
        for title, due_date in (
            ('PLAT-1147-shaped', '2026-09-14T20:13:48.534977+00:00'),
            ('PLAT-1149-shaped', '2026-10-03T20:53:11.921920+00:00'),
        ):
            self.item(title, due_date=datetime.fromisoformat(due_date))
        result = self.preflight()
        self.assertEqual((result['decision'], result['reason']), ('skip', 'future_due'))
        self.assertEqual(result['ready_task_count'], 0)
        self.assertEqual(result['future_due_count'], 1)

    def test_live_reviewed_task_rows_are_waiting_in_canonical_owner_projection(self):
        self.assert_waiting(self.item('Future reviewed work'), True)

    def test_custom_initial_slug_and_display_name_use_workflow_metadata(self):
        workflow = self.custom_workflow('recurring-owner-custom')
        for identifier in (workflow.slug, workflow.name.upper()):
            with self.subTest(identifier=identifier):
                item = self.item(
                    identifier, workflow_name=identifier,
                    status=workflow.initial_status.slug,
                )
                self.assert_waiting(item, True)
        self.assertEqual(self.preflight()['reason'], 'future_due')

    def test_same_status_in_another_workflow_is_not_an_initial_state(self):
        workflow = self.custom_workflow('recurring-owner-shared')
        other = Workflow.objects.get(slug='reviewed-task')
        WorkflowStatus.objects.create(workflow=other, status=workflow.initial_status)
        item = self.item('Noninitial custom work', status=workflow.initial_status.slug)
        self.assert_waiting(item, False)
        self.assertEqual(self.preflight()['reason'], 'ready_task')

    def test_explicit_active_stage_remains_runnable_with_future_due(self):
        item = self.item('In-flight work', status='in_progress')
        self.assert_waiting(item, False)
        self.assertEqual(self.preflight()['reason'], 'ready_task')
        workflow = self.custom_workflow('recurring-owner-active', category='in_progress')
        Item.objects.filter(pk=item.pk).update(
            workflow_name=workflow.slug, status=workflow.initial_status.slug,
        )
        self.assert_waiting(item, False)
        self.assertEqual(self.preflight()['reason'], 'ready_task')

    def test_exact_due_boundary_uses_aware_instant_not_local_date(self):
        item = self.item('Boundary work')
        local_now = self.now.astimezone(datetime_timezone(timedelta(hours=5, minutes=30)))
        with timezone.override('Asia/Kolkata'):
            for offset, waiting in ((1, True), (0, False), (-1, False)):
                with self.subTest(offset=offset):
                    Item.objects.filter(pk=item.pk).update(
                        due_date=local_now + timedelta(microseconds=offset),
                    )
                    self.assert_waiting(item, waiting)
                    self.assertEqual(
                        self.preflight()['reason'], 'future_due' if waiting else 'ready_task',
                    )

    def test_ordinary_future_deadline_and_missing_due_remain_runnable(self):
        ordinary = self.item('Ordinary deadline', is_recurring=False)
        undated = self.item('Undated recurring work', due_date=None)
        self.assert_waiting(ordinary, False)
        self.assert_waiting(undated, False)
        self.assertEqual(self.preflight()['reason'], 'ready_task')

    def test_legacy_open_compatibility_does_not_classify_unknown_todo(self):
        legacy = self.item('Legacy open', workflow_name='missing-workflow', status='open')
        self.assert_waiting(legacy, True)
        self.assertEqual(self.preflight()['reason'], 'future_due')
        unknown = self.item('Unknown todo', workflow_name='missing-workflow')
        self.assert_waiting(unknown, False)
        self.assertEqual(self.preflight()['reason'], 'ready_task')

    def test_scheduled_future_is_still_parked_and_review_is_not_owner_ready(self):
        scheduled = self.item('Timer-owned gap', status='scheduled')
        self.assertFalse(filter_runnable_owner_queue_items(Item.objects.filter(pk=scheduled.pk)).exists())
        self.assertEqual(self.preflight()['decision'], 'skip')
        review = self.item('In review', status='in_review')
        self.assert_waiting(review, False)
        self.assertEqual(self.preflight()['decision'], 'skip')

    def test_real_queue_and_focus_guard_allow_ordinary_work_ahead_of_future_initial(self):
        future = self.item('Urgent future recurrence')
        ready = self.item('Current owner work', is_recurring=False, priority='normal')
        response = self.client.get('/api/items/', {
            'mode': 'task', 'assignee_email': self.agent.email,
            'queue_order': 'true', 'is_active': 'true', 'permission_hint': 'false',
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([row['id'] for row in response.data['results']], [ready.pk, future.pk])
        response = self.client.post('/api/comments/', {
            'item': ready.pk, 'content': 'Advance current owner work.',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.content)

    def test_mcp_real_api_roundtrip_and_preflight_agree_about_future_initial(self):
        # The MCP-side default-active filter (_filter_default_active_items)
        # compares due dates against REAL wall clock, not the mocked Django
        # clock — so the seeded "future" due must be future relative to BOTH
        # clocks, or the item stops being hidden once wall time passes the
        # hardcoded due (the 2026-09-15 CI red). max() of the two clocks + 2d
        # is future for either.
        wall_now = datetime.now(datetime_timezone.utc)
        seed_due = max(self.now, wall_now) + timedelta(days=2)
        self.item('Future recurring review', due_date=seed_due)
        mcp_dir = str(Path(__file__).resolve().parents[2] / 'mcp-server')
        if mcp_dir not in sys.path:
            sys.path.insert(0, mcp_dir)
        import pulse_server

        def api_request(method, endpoint, *, params=None, **kwargs):
            self.assertEqual(method, 'GET')
            response = self.client.get('/api/' + endpoint.lstrip('/'), params or {})
            self.assertEqual(response.status_code, 200, response.content)
            return response.data

        with mock.patch.object(pulse_server, '_get_my_user_id', return_value=(self.agent.pk, self.agent.email)), \
             mock.patch.object(pulse_server, 'api_request', side_effect=api_request):
            result = pulse_server.get_my_tasks()
        self.assertNotIn('1 ready/movable', result)
        self.assertIn('not yet due', result)
        self.assertEqual(self.preflight()['reason'], 'future_due')

    def test_repeated_real_preflight_rearms_only_when_state_or_due_changes(self):
        item = self.item('Recurring transition sequence')
        for task_status, due_date, reason in (
            ('todo', item.due_date, 'future_due'),
            ('in_progress', item.due_date, 'ready_task'),
            ('todo', item.due_date, 'future_due'),
            ('todo', self.now, 'ready_task'),
        ):
            Item.objects.filter(pk=item.pk).update(status=task_status, due_date=due_date)
            for attempt in range(2):
                with self.subTest(status=task_status, reason=reason, attempt=attempt):
                    self.assertEqual(self.preflight()['reason'], reason)
                    item.refresh_from_db()
                    self.assertEqual(item.status, task_status)
                    self.assertEqual(item.due_date, due_date)
                    self.assertEqual(item.assignee_id, self.agent.pk)

    def test_owner_and_organization_scoped_queryset_is_not_widened(self):
        own_project = Project.objects.create(key='RCOWN', name='Owner project', org_id=1)
        other_project = Project.objects.create(key='RCOTHER', name='Other project', org_id=2)
        for org_id in (1, 2):
            TeamRoster.objects.create(
                team_name='recurring-owner-team', organization_id=org_id,
                organization_slug='shizuha' if org_id == 1 else 'shizuha-digital',
                tiers=[[self.agent.email, 'other@shizuha.com']], is_active=True,
            )
        future = self.item(
            'Scoped future recurrence', project=own_project,
            assignment_group='recurring-owner-team',
        )
        self.item(
            'Other organization', is_recurring=False, project=other_project,
            assignment_group='recurring-owner-team',
        )
        self.item(
            'Other owner', is_recurring=False, project=own_project,
            assignee_email='other@shizuha.com', assignee_id=None,
            assignment_group='recurring-owner-team',
        )
        scoped = Item.objects.filter(project__org_id=1, assignee_email=self.agent.email)
        self.assertEqual(list(scoped.values_list('pk', flat=True)), [future.pk])
        self.assertFalse(filter_runnable_owner_queue_items(scoped).exists())
        # Operator 2026-09-24: the param is advisory for non-privileged
        # callers — the probe returns the CALLER's own decision (this agent
        # has no runnable work, so it must NOT run on other@shizuha.com's
        # queue either; no 403 identity oracle, no cross-owner leak).
        probe = self.client.get('/api/items/heartbeat-preflight/', {
            'assignee_email': 'other@shizuha.com',
        })
        own = self.client.get('/api/items/heartbeat-preflight/', {
            'assignee_email': self.agent.email,
        })
        self.assertEqual(probe.status_code, 200)
        self.assertEqual(own.status_code, 200)
        # Param-independence: the probe must yield exactly the caller's own
        # token-identity decision — 'other@shizuha.com' never influences it.
        self.assertEqual(
            probe.json().get('decision'), own.json().get('decision'),
        )
        self.assertEqual(
            probe.json().get('ready_task_count'), own.json().get('ready_task_count'),
        )
