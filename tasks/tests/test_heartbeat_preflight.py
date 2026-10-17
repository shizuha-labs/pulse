"""Bounded zero-token scheduler preflight for agent heartbeats."""

from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from tasks.models import EscalationRoutingDisposition, Item, Project
from .base import create_blocked_item


User = get_user_model()
URL = '/api/items/heartbeat-preflight/'


class HeartbeatPreflightTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.agent = User.objects.create_user(
            username='preflight-agent',
            email='preflight-agent@shizuha.com',
            password='x',
            is_staff=False,
            is_superuser=False,
        )
        self.client.force_authenticate(user=self.agent)

    def _get(self, email=None):
        return self.client.get(
            URL,
            {'assignee_email': email or self.agent.email},
        )

    def _task(self, title, **kwargs):
        kwargs.setdefault('mode', 'task')
        kwargs.setdefault('status', 'open')
        kwargs.setdefault('workflow_name', 'autonomous-bug')
        kwargs.setdefault('assignee_id', self.agent.id)
        kwargs.setdefault('assignee_email', self.agent.email)
        return Item.objects.create(title=title, **kwargs)

    def test_unassigned_team_unstarted_wakes_when_personal_queue_empty(self):
        from tasks.models import TeamRoster
        TeamRoster.objects.create(
            team_name='preflight-security',
            organization_id=1,
            organization_slug='shizuha',
            tiers=[[self.agent.email]],
            is_active=True,
        )
        Item.objects.create(
            title='Unassigned security triage',
            project=Project.objects.create(name='Security', key='PFSEC', org_id=1),
            mode='task',
            status='needs_triage',
            workflow_name='security-finding',
            assignment_group='preflight-security',
        )
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.data['decision'], response.data['reason']),
            ('run', 'team_unstarted'),
        )
        self.assertEqual(response.data['ready_task_count'], 1)

    def test_same_team_name_in_other_org_does_not_wake_agent(self):
        from tasks.models import TeamRoster
        TeamRoster.objects.create(
            team_name='devops', organization_id=3,
            organization_slug='shizuha-trading', tiers=[[self.agent.email]],
            is_active=True,
        )
        # A different team in org1 must not create an org/team cross-product.
        TeamRoster.objects.create(
            team_name='preflight-qa', organization_id=1,
            organization_slug='shizuha', tiers=[[self.agent.email]],
            is_active=True,
        )
        foreign = Project.objects.create(name='Other org', key='PFOTHER', org_id=1)
        Item.objects.create(
            title='Another organization devops task', project=foreign,
            mode='task', status='todo', workflow_name='autonomous-bug',
            assignment_group='devops',
        )
        response = self._get()
        self.assertEqual(
            (response.data['decision'], response.data['reason']),
            ('skip', 'queue_empty'),
        )
        own = Project.objects.create(name='Own org', key='PFOWN', org_id=3)
        Item.objects.create(
            title='Own organization devops task', project=own,
            mode='task', status='todo', workflow_name='autonomous-bug',
            assignment_group='devops',
        )
        response = self._get()
        self.assertEqual(
            (response.data['decision'], response.data['reason']),
            ('run', 'team_unstarted'),
        )

    def test_junior_alias_keeps_organization_scoped_team_wake(self):
        from tasks.models import TeamRoster
        TeamRoster.objects.create(
            team_name='preflight-qa', organization_id=3,
            organization_slug='shizuha-trading', tiers=[],
            junior_emails=[self.agent.email],
            is_active=True,
        )
        self.agent.email = self.agent.email.replace('@shizuha.com', '@agents.shizuha.io')
        own = Project.objects.create(name='Junior org', key='PFJUNIOR', org_id=3)
        Item.objects.create(
            title='Junior team work', project=own, mode='task', status='todo',
            workflow_name='autonomous-bug', assignment_group='preflight-qa',
        )
        response = self._get()
        self.assertEqual(
            (response.data['decision'], response.data['reason']),
            ('run', 'team_unstarted'),
        )

    def test_personal_in_review_does_not_wake_idle_heartbeat(self):
        """Scout 2026-08-31: personal in_review is a handoff, not ready work."""
        self._task(
            'Waiting on review',
            status='in_review',
            workflow_name='autonomous-dev',
        )
        response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            (response.data['decision'], response.data['reason']),
            ('skip', 'queue_empty'),
        )

    def test_seated_team_in_review_does_not_wake_on_empty_personal_queue(self):
        from tasks.models import TeamRoster
        TeamRoster.objects.create(
            team_name='preflight-review',
            organization_id=1,
            organization_slug='shizuha',
            tiers=[[self.agent.email]],
            is_active=True,
        )
        Item.objects.create(
            title='Already in review',
            mode='task',
            status='in_review',
            workflow_name='autonomous-bug',
            assignment_group='preflight-review',
            assignee_id=self.agent.id + 1,
            assignee_email='reika@shizuha.com',
        )
        response = self._get()
        self.assertEqual(
            (response.data['decision'], response.data['reason']),
            ('skip', 'queue_empty'),
        )

    def test_empty_queue_skips_without_list_query_machinery(self):
        with CaptureQueriesContext(connection) as queries:
            response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['decision'], 'skip')
        self.assertEqual(response.data['reason'], 'queue_empty')
        self.assertTrue(response.data['counts_are_presence_markers'])

        sql = '\n'.join(query['sql'].lower() for query in queries.captured_queries)
        item_sql = '\n'.join(
            query['sql'].lower()
            for query in queries.captured_queries
            if 'tasks_task' in query['sql'].lower()
        )
        self.assertNotIn('select count(', sql)
        self.assertNotIn('select distinct', sql)
        self.assertNotIn('order by', item_sql)
        self.assertLessEqual(len(queries), 10, sql)

    def test_alert_ready_task_and_backlog_each_wake_the_agent(self):
        alert = Item.objects.create(
            mode='alert',
            title='Active security alert',
            status='firing',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
        )
        response = self._get()
        self.assertEqual(
            (response.data['decision'], response.data['reason']),
            ('run', 'active_alert'),
        )
        alert.delete()

        ready = self._task('Ready task', status='in_progress')
        response = self._get()
        self.assertEqual(
            (response.data['decision'], response.data['reason']),
            ('run', 'ready_task'),
        )
        ready.delete()

        backlog = self._task('Backlog pull')
        Item.objects.filter(pk=backlog.pk).update(status='backlog')
        response = self._get()
        self.assertEqual(
            (response.data['decision'], response.data['reason']),
            ('run', 'backlog_pull'),
        )

    def test_blocked_only_queue_skips(self):
        create_blocked_item(
            title='Waiting on dependency',
            mode='task',
            workflow_name='autonomous-bug',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
        )

        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['decision'], 'skip')
        self.assertEqual(response.data['reason'], 'all_blocked')
        self.assertEqual(response.data['blocked_task_count'], 1)

    def test_blocked_seat_does_not_wake_for_backlog_pull(self):
        create_blocked_item(
            title='Waiting on dependency',
            mode='task',
            workflow_name='autonomous-bug',
            assignee_id=self.agent.id,
            assignee_email=self.agent.email,
        )
        backlog = self._task('Backlog pull')
        Item.objects.filter(pk=backlog.pk).update(status='backlog')

        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['decision'], 'skip')
        self.assertEqual(response.data['reason'], 'all_blocked')
        self.assertEqual(response.data['blocked_task_count'], 1)

    def _held_item(self, *, reason, status='in_progress', generation=1):
        project = Project.objects.create(
            name=f'Preflight {reason} {generation}',
            key=f'P{generation}{reason[:6].upper()}',
            org_id=1,
        )
        item = self._task(f'Held {reason}', status=status)
        EscalationRoutingDisposition.objects.create(
            organization_id=1,
            project=project,
            item=item,
            transition_id=uuid4(),
            source_generation=generation - 1,
            generation=generation,
            disposition_digest=f'{reason}-{generation}'.ljust(64, '0')[:64],
            schema_version='shizuha.pulse.escalation-evidence.v1',
            classification='mixed_or_unknown_escalation_hold',
            outcome='held',
            reason_code=reason,
        )
        return item

    def test_unresolved_operator_destination_hold_skips_without_backlog_pull(self):
        self._held_item(reason='org_local_operator_destination_unresolved')
        backlog = self._task('Backlog pull')
        Item.objects.filter(pk=backlog.pk).update(status='backlog')

        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['decision'], 'skip')
        self.assertEqual(response.data['reason'], 'all_blocked')

    def test_schema_repair_hold_still_wakes(self):
        self._held_item(reason='missing_evidence_schema')

        response = self._get()

        self.assertEqual(
            (response.data['decision'], response.data['reason']),
            ('run', 'ready_task'),
        )

    def test_future_recurring_open_queue_skips_but_active_wip_wakes(self):
        future = timezone.now() + timedelta(days=2)
        item = self._task(
            'Future recurring occurrence',
            is_recurring=True,
            frequency='daily',
            due_date=future,
        )

        response = self._get()
        self.assertEqual(
            (response.data['decision'], response.data['reason']),
            ('skip', 'future_due'),
        )

        item.status = 'in_progress'
        item.save(update_fields=['status'])
        response = self._get()
        self.assertEqual(
            (response.data['decision'], response.data['reason']),
            ('run', 'ready_task'),
        )

    def test_alias_owner_is_visible_and_legacy_id_fallback_is_bounded_to_self(self):
        self._task(
            'Alias-owned task',
            assignee_id=None,
            assignee_email='preflight-agent@agents.shizuha.io',
        )
        response = self._get()
        self.assertEqual(response.data['reason'], 'ready_task')

        Item.objects.all().delete()
        self._task('Legacy id-only task', assignee_email=None)
        response = self._get()
        self.assertEqual(response.data['reason'], 'ready_task')

    def test_non_staff_cross_owner_probe_returns_own_decision(self):
        # Operator 2026-09-24: the param is advisory for non-privileged
        # callers — a cross-owner probe cannot see another agent's queue and
        # cannot even use a 403 as an identity oracle; it always gets the
        # CALLER's own decision computed from the signed token identity.
        response = self._get('another-agent@shizuha.com')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'another-agent', response.content)

    def test_staff_can_verify_another_owner(self):
        staff = User.objects.create_user(
            username='preflight-staff',
            email='staff@shizuha.com',
            password='x',
            is_staff=True,
        )
        self.client.force_authenticate(user=staff)
        self._task(
            'Other owner task',
            assignee_id=None,
            assignee_email='another-agent@shizuha.com',
        )

        response = self._get('another-agent@shizuha.com')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['reason'], 'ready_task')


class HeartbeatPreflightTokenIdentityTests(APITestCase):
    """Operator 2026-09-24: the scheduler decision runs on the SIGNED TOKEN
    identity, never on what the request claims, and sees work across ALL
    orgs — no org scoping on the agent work path."""

    def setUp(self):
        self.client = APIClient()
        self.agent = User.objects.create_user(
            username='token-id-agent',
            email='token-id-agent@shizuha.com',
            password='x',
            is_staff=False,
            is_superuser=False,
        )
        self.other = User.objects.create_user(
            username='other-agent',
            email='other-agent@shizuha.com',
            password='x',
        )
        self.client.force_authenticate(user=self.agent)

    def _task(self, title, **kwargs):
        kwargs.setdefault('mode', 'task')
        kwargs.setdefault('status', 'open')
        kwargs.setdefault('workflow_name', 'autonomous-bug')
        return Item.objects.create(title=title, **kwargs)

    def test_mismatched_assignee_email_param_is_ignored_for_non_privileged(self):
        # A stale or hostile assignee_email param must not point the scheduler
        # at another identity's (or an empty) queue — token identity wins.
        self._task('real work', assignee_id=self.agent.id,
                   assignee_email=self.agent.email)
        Item.objects.create(
            title='other agent work', mode='task', status='open',
            workflow_name='autonomous-bug',
            assignee_id=self.other.id, assignee_email=self.other.email,
        )
        response = self.client.get(
            URL, {'assignee_email': 'other-agent@shizuha.com'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get('decision'), 'run')
        self.assertEqual(payload.get('reason'), 'ready_task')

    def test_preflight_lookup_has_no_org_scope_filter(self):
        # Cross-org assignment is DB-isolated by design (the
        # cross_organization_assignment_denied constraint) — an agent can only
        # HOLD work in orgs where it is a member. The FETCH path, however,
        # must not add any org filter of its own: token identity in, all that
        # identity's work out (production proof: ryo's snapshot spans
        # shizuha::merge AND org1::merge simultaneously). Assert the owner
        # filter the view builds contains no organization predicate.
        from tasks.views import ItemViewSet
        import inspect
        src = inspect.getsource(ItemViewSet.heartbeat_preflight)
        owner_lookup = src.split('owner_items =', 1)[1].split('def response_payload', 1)[0]
        self.assertNotIn('organization', owner_lookup)
        self.assertNotIn('org_id', owner_lookup)
        self.assertIn('email_variants', src)

