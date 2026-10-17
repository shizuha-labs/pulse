"""
Exhaustive E2E tests for Phase 5: Notification Schemes, SLA Management, Business Calendars.
"""
from rest_framework import status as http_status
from tasks.models import (
    NotificationScheme, NotificationSchemeEvent,
    SLADefinition, BusinessCalendar, SLAInstance,
    Project, Item,
)
from .base import AuthenticatedAPITestCase


class NotificationSchemeAPITests(AuthenticatedAPITestCase):
    """CRUD + event tests for NotificationScheme API."""

    def test_seeded_scheme_exists(self):
        resp = self.client.get('/shizuha-pulse/api/notification-schemes/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        slugs = [s['slug'] for s in results]
        self.assertIn('default', slugs)

    def test_default_scheme_has_events(self):
        scheme = NotificationScheme.objects.get(slug='default')
        self.assertGreater(scheme.events.count(), 0)

    def test_create_notification_scheme(self):
        resp = self.client.post('/shizuha-pulse/api/notification-schemes/', {
            'name': 'Custom Notifications',
            'slug': 'custom-notif',
            'description': 'Custom routing',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_add_event(self):
        scheme = NotificationScheme.objects.create(
            name='Event Test', slug='event-test-notif'
        )
        resp = self.client.post(
            f'/shizuha-pulse/api/notification-schemes/{scheme.id}/add_event/',
            {
                'event_type': 'issue_created',
                'recipient_type': 'reporter',
            }
        )
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(scheme.events.count(), 1)

    def test_add_multiple_events_same_type(self):
        """Same event can notify multiple recipient types."""
        scheme = NotificationScheme.objects.create(
            name='Multi Event', slug='multi-event-notif'
        )
        recipients = ['assignee', 'reporter', 'watchers', 'project_lead']
        for rt in recipients:
            resp = self.client.post(
                f'/shizuha-pulse/api/notification-schemes/{scheme.id}/add_event/',
                {'event_type': 'issue_resolved', 'recipient_type': rt}
            )
            self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(scheme.events.filter(event_type='issue_resolved').count(), 4)

    def test_all_19_event_types_valid(self):
        """Verify all 19 event types exist."""
        expected = [
            'issue_created', 'issue_updated', 'issue_assigned', 'issue_resolved',
            'issue_closed', 'issue_reopened', 'issue_deleted', 'issue_moved',
            'comment_created', 'comment_edited', 'comment_deleted',
            'attachment_created', 'worklog_created', 'worklog_updated', 'worklog_deleted',
            'sprint_started', 'sprint_completed', 'version_released', 'status_changed',
        ]
        valid = [c[0] for c in NotificationSchemeEvent.EVENT_TYPE_CHOICES]
        for et in expected:
            self.assertIn(et, valid)
        self.assertEqual(len(valid), 19)

    def test_all_8_recipient_types_valid(self):
        expected = ['assignee', 'reporter', 'project_lead', 'watchers',
                     'role', 'group', 'user', 'component_lead']
        valid = [c[0] for c in NotificationSchemeEvent.RECIPIENT_TYPE_CHOICES]
        for rt in expected:
            self.assertIn(rt, valid)
        self.assertEqual(len(valid), 8)

    def test_remove_event(self):
        scheme = NotificationScheme.objects.create(
            name='Remove Event', slug='remove-event-notif'
        )
        event = NotificationSchemeEvent.objects.create(
            scheme=scheme, event_type='issue_created', recipient_type='reporter'
        )
        resp = self.client.post(
            f'/shizuha-pulse/api/notification-schemes/{scheme.id}/remove_event/',
            {'event_id': str(event.id)}
        )
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertEqual(scheme.events.count(), 0)

    def test_role_based_notification(self):
        """Add event with role + value."""
        scheme = NotificationScheme.objects.create(
            name='Role Notif', slug='role-notif'
        )
        resp = self.client.post(
            f'/shizuha-pulse/api/notification-schemes/{scheme.id}/add_event/',
            {
                'event_type': 'issue_assigned',
                'recipient_type': 'role',
                'recipient_value': 'developer',
            }
        )
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        event = scheme.events.first()
        self.assertEqual(event.recipient_value, 'developer')

    def test_delete_notification_scheme(self):
        scheme = NotificationScheme.objects.create(name='Del', slug='del-notif')
        resp = self.client.delete(f'/shizuha-pulse/api/notification-schemes/{scheme.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)


class BusinessCalendarAPITests(AuthenticatedAPITestCase):
    """CRUD tests for BusinessCalendar API."""

    def test_seeded_calendar_exists(self):
        resp = self.client.get('/shizuha-pulse/api/business-calendars/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        slugs = [c['slug'] for c in results]
        self.assertIn('standard', slugs)

    def test_standard_calendar_hours(self):
        cal = BusinessCalendar.objects.get(slug='standard')
        self.assertEqual(cal.timezone, 'UTC')
        # Mon-Fri 09:00-17:00
        for day in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']:
            self.assertIn(day, cal.working_hours)
            self.assertEqual(cal.working_hours[day]['start'], '09:00')
            self.assertEqual(cal.working_hours[day]['end'], '17:00')
        # No weekend
        for day in ['saturday', 'sunday']:
            self.assertNotIn(day, cal.working_hours)

    def test_create_custom_calendar(self):
        resp = self.client.post('/shizuha-pulse/api/business-calendars/', {
            'name': 'India Business Hours',
            'slug': 'india-hours',
            'timezone': 'Asia/Kolkata',
            'working_hours': {
                'monday': {'start': '10:00', 'end': '19:00'},
                'tuesday': {'start': '10:00', 'end': '19:00'},
                'wednesday': {'start': '10:00', 'end': '19:00'},
                'thursday': {'start': '10:00', 'end': '19:00'},
                'friday': {'start': '10:00', 'end': '19:00'},
            },
            'holidays': [
                {'date': '2026-01-26', 'name': 'Republic Day'},
                {'date': '2026-08-15', 'name': 'Independence Day'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['timezone'], 'Asia/Kolkata')
        self.assertEqual(len(resp.json()['holidays']), 2)

    def test_24x7_calendar(self):
        """Calendar with no working hours = 24/7."""
        resp = self.client.post('/shizuha-pulse/api/business-calendars/', {
            'name': '24/7 Support',
            'slug': '24x7',
            'timezone': 'UTC',
            'working_hours': {
                'monday': {'start': '00:00', 'end': '23:59'},
                'tuesday': {'start': '00:00', 'end': '23:59'},
                'wednesday': {'start': '00:00', 'end': '23:59'},
                'thursday': {'start': '00:00', 'end': '23:59'},
                'friday': {'start': '00:00', 'end': '23:59'},
                'saturday': {'start': '00:00', 'end': '23:59'},
                'sunday': {'start': '00:00', 'end': '23:59'},
            },
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_update_calendar(self):
        cal = BusinessCalendar.objects.create(
            name='Upd', slug='upd-cal', timezone='UTC',
            working_hours={'monday': {'start': '09:00', 'end': '17:00'}}
        )
        resp = self.client.patch(f'/shizuha-pulse/api/business-calendars/{cal.id}/', {
            'timezone': 'America/New_York',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        cal.refresh_from_db()
        self.assertEqual(cal.timezone, 'America/New_York')

    def test_delete_calendar(self):
        cal = BusinessCalendar.objects.create(
            name='Del', slug='del-cal', timezone='UTC'
        )
        resp = self.client.delete(f'/shizuha-pulse/api/business-calendars/{cal.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)


class SLADefinitionAPITests(AuthenticatedAPITestCase):
    """CRUD tests for SLADefinition API."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='SLA Project', key='SLA', org_id=1
        )
        self.calendar = BusinessCalendar.objects.get(slug='standard')

    def test_create_sla_definition(self):
        resp = self.client.post('/shizuha-pulse/api/sla-definitions/', {
            'project': self.project.id,
            'name': 'P1 Bug Response',
            'metric': 'time_to_first_response',
            'targets': {'urgent': 60, 'high': 240, 'normal': 480, 'low': 1440},
            'calendar': str(self.calendar.id),
            'jql_filter': 'priority = urgent AND type = Bug',
            'conditions': {
                'start': ['open', 'reopened'],
                'pause': ['blocked'],
                'stop': ['done', 'closed'],
            },
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['metric'], 'time_to_first_response')
        self.assertEqual(resp.json()['targets']['urgent'], 60)

    def test_all_5_metrics_valid(self):
        expected = ['time_to_first_response', 'time_to_resolution',
                     'time_to_assignee', 'time_in_status', 'time_to_close']
        valid = [c[0] for c in SLADefinition.METRIC_CHOICES]
        for m in expected:
            self.assertIn(m, valid)
        self.assertEqual(len(valid), 5)

    def test_create_sla_without_calendar(self):
        """SLA without calendar = 24/7 tracking."""
        resp = self.client.post('/shizuha-pulse/api/sla-definitions/', {
            'project': self.project.id,
            'name': '24/7 Resolution',
            'metric': 'time_to_resolution',
            'targets': {'urgent': 120, 'high': 480},
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertIsNone(resp.json().get('calendar'))

    def test_filter_sla_by_project(self):
        SLADefinition.objects.create(
            project=self.project, name='Test SLA',
            metric='time_to_first_response', targets={'high': 240}
        )
        resp = self.client.get(f'/shizuha-pulse/api/sla-definitions/?project={self.project.id}')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        self.assertGreaterEqual(len(results), 1)

    def test_update_sla(self):
        sla = SLADefinition.objects.create(
            project=self.project, name='Upd SLA',
            metric='time_to_resolution', targets={'high': 480}
        )
        resp = self.client.patch(f'/shizuha-pulse/api/sla-definitions/{sla.id}/', {
            'targets': {'urgent': 60, 'high': 240},
            'is_active': False,
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        sla.refresh_from_db()
        self.assertFalse(sla.is_active)
        self.assertEqual(sla.targets['urgent'], 60)

    def test_delete_sla(self):
        sla = SLADefinition.objects.create(
            project=self.project, name='Del SLA',
            metric='time_to_close', targets={}
        )
        resp = self.client.delete(f'/shizuha-pulse/api/sla-definitions/{sla.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)


class SLAInstanceAPITests(AuthenticatedAPITestCase):
    """Read-only tests for SLAInstance API."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='SLA Inst Proj', key='SLAI', org_id=1
        )
        self.item = Item.objects.create(
            mode='task', title='SLA Task', project=self.project,
            item_key='SLAI-1', status='pending', created_by_id=self.user.id
        )
        self.sla_def = SLADefinition.objects.create(
            project=self.project, name='Response SLA',
            metric='time_to_first_response',
            targets={'high': 240}
        )

    def test_create_and_list_sla_instance(self):
        """SLA instances are created automatically, but we can verify listing."""
        instance = SLAInstance.objects.create(
            item=self.item, sla_definition=self.sla_def,
            target_minutes=240, sla_status='active',
            elapsed_minutes=30, remaining_minutes=210
        )
        resp = self.client.get('/shizuha-pulse/api/sla-instances/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        self.assertGreaterEqual(len(results), 1)

    def test_sla_instance_is_readonly(self):
        """SLA instances should not be creatable via API (read-only viewset)."""
        resp = self.client.post('/shizuha-pulse/api/sla-instances/', {
            'item': self.item.id,
            'sla_definition': str(self.sla_def.id),
            'target_minutes': 240,
        }, format='json')
        self.assertIn(resp.status_code, [
            http_status.HTTP_405_METHOD_NOT_ALLOWED,
            http_status.HTTP_403_FORBIDDEN,
        ])

    def test_sla_instance_breach_tracking(self):
        """Create a breached SLA instance and verify fields."""
        from django.utils import timezone
        instance = SLAInstance.objects.create(
            item=self.item, sla_definition=self.sla_def,
            target_minutes=60, sla_status='breached',
            elapsed_minutes=90, is_breached=True,
            breached_at=timezone.now(),
            warning_sent=True,
        )
        resp = self.client.get(f'/shizuha-pulse/api/sla-instances/{instance.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertTrue(resp.json()['is_breached'])
        self.assertEqual(resp.json()['sla_status'], 'breached')

    def test_filter_by_item(self):
        SLAInstance.objects.create(
            item=self.item, sla_definition=self.sla_def,
            target_minutes=120, sla_status='active'
        )
        resp = self.client.get(f'/shizuha-pulse/api/sla-instances/?item={self.item.id}')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

    def test_all_5_sla_statuses(self):
        """Verify all SLA status choices."""
        expected = ['active', 'paused', 'met', 'breached', 'stopped']
        valid = [c[0] for c in SLAInstance.SLA_STATUS_CHOICES]
        for s in expected:
            self.assertIn(s, valid)
