"""
PLAT-4195 regression — cross-tenant / IDOR authorization on the item bulk-op
endpoint and the comment / work-log / custom-field-value read endpoints.

Live-confirmed before the fix: a non-staff user in org A could mass-delete
another org's items via POST /api/items/bulk/ (raw Item.objects.filter(id_q),
no tenant filter), and could read another org's comments, work logs, and
custom-field values by id/param. These tests pin the fix: bulk ops and reads
resolve against PermissionService.get_user_item_queryset so a caller can only
reach items in their own orgs/projects (or that they own).
"""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient

from tasks.models import (
    Comment,
    ExternalDependencyBinding,
    ExternalDependencyReleaseEvent,
    ExternalWaitAuthority,
    Item,
    Project,
    WorkLog,
)
from tasks.services.external_wait import (
    accept_cross_org_wait,
    prepare_cross_org_wait,
    process_release_event,
    record_provider_release,
    revoke_cross_org_wait,
)
from tasks.services.actionable_items import filter_active_queue_items
from tasks.serializers import ItemSerializer

User = get_user_model()


class PLS903CrossOrgExternalWaitRolloutTests(SimpleTestCase):
    def test_admission_is_enabled_by_default(self):
        self.assertIs(settings.PULSE_CROSS_ORG_EXTERNAL_WAIT_ENABLED, True)


class PLAT4195CrossTenantIDORTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        # Two non-staff users, each a member of exactly ONE distinct org.
        self.user_a = User.objects.create_user(
            username='tenant_a', email='a@sec.test', password='x', is_staff=False)
        self.user_a.organization_memberships = {1: 'owner'}
        self.user_b = User.objects.create_user(
            username='tenant_b', email='b@sec.test', password='x', is_staff=False)
        self.user_b.organization_memberships = {2: 'owner'}

        self.proj_a = Project.objects.create(
            name='OrgA', key='ORGA', org_id=1, access_level='private', is_active=True)
        self.proj_b = Project.objects.create(
            name='OrgB', key='ORGB', org_id=2, access_level='private', is_active=True)
        self.item_a = Item.objects.create(mode='task', title='A-secret', project=self.proj_a)
        self.item_b = Item.objects.create(mode='task', title='B-secret', project=self.proj_b)

        # B-owned satellite rows for the read tests.
        self.comment_b = Comment.objects.create(
            item=self.item_b, author_id=self.user_b.id, author_email='b@sec.test', content='b-note')
        self.worklog_b = WorkLog.objects.create(
            item=self.item_b, user_id=self.user_b.id, user_email='b@sec.test',
            duration_minutes=30, worked_date='2026-07-11')

    # ---- CRITICAL: items/bulk cross-tenant write/delete ----
    def test_bulk_delete_cannot_reach_other_orgs_item(self):
        self.client.force_authenticate(self.user_a)
        resp = self.client.post(
            '/api/items/bulk/',
            {'item_ids': [self.item_b.id], 'operation': 'delete'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get('deleted'), 0)
        self.assertTrue(Item.objects.filter(id=self.item_b.id).exists(),
                        "org A deleted org B's item via bulk endpoint (IDOR)")

    def test_bulk_transition_cannot_reach_other_orgs_item(self):
        self.client.force_authenticate(self.user_a)
        resp = self.client.post(
            '/api/items/bulk/',
            {'item_ids': [self.item_b.id], 'operation': 'transition',
             'data': {'to_status': 'done'}}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get('updated'), 0)
        self.item_b.refresh_from_db()
        self.assertNotEqual(self.item_b.status, 'done')

    def test_bulk_delete_still_works_on_own_item(self):
        self.client.force_authenticate(self.user_a)
        resp = self.client.post(
            '/api/items/bulk/',
            {'item_ids': [self.item_a.id], 'operation': 'delete'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data.get('deleted'), 1)
        self.assertFalse(Item.objects.filter(id=self.item_a.id).exists())

    # ---- HIGH: cross-tenant reads ----
    def test_comment_detail_cross_tenant_blocked(self):
        self.client.force_authenticate(self.user_a)
        resp = self.client.get(f'/api/comments/{self.comment_b.id}/')
        self.assertIn(resp.status_code, (403, 404))

    def test_comment_reactions_cross_tenant_blocked(self):
        self.client.force_authenticate(self.user_a)
        resp = self.client.get(f'/api/comments/{self.comment_b.id}/reactions/')
        self.assertIn(resp.status_code, (403, 404))

    def test_worklog_by_item_cross_tenant_blocked(self):
        self.client.force_authenticate(self.user_a)
        resp = self.client.get(f'/api/work-logs/by_item/?item={self.item_b.id}')
        self.assertIn(resp.status_code, (403, 404))

    def test_worklog_by_user_id_does_not_leak_cross_tenant(self):
        self.client.force_authenticate(self.user_a)
        resp = self.client.get(f'/api/work-logs/?user_id={self.user_b.id}')
        self.assertEqual(resp.status_code, 200)
        rows = resp.data.get('results') if isinstance(resp.data, dict) else resp.data
        rows = rows or []
        ids = [w.get('id') for w in rows if isinstance(w, dict)]
        self.assertNotIn(self.worklog_b.id, ids,
                         "org A read org B's work logs via ?user_id (IDOR)")

    def test_own_worklog_still_visible(self):
        WorkLog.objects.create(
            item=self.item_a, user_id=self.user_a.id, user_email='a@sec.test',
            duration_minutes=15, worked_date='2026-07-11')
        self.client.force_authenticate(self.user_a)
        resp = self.client.get('/api/work-logs/')
        self.assertEqual(resp.status_code, 200)

    def test_custom_field_values_cross_tenant_blocked(self):
        self.client.force_authenticate(self.user_a)
        resp = self.client.get(f'/api/custom-field-values/?item_id={self.item_b.id}')
        self.assertEqual(resp.status_code, 200)
        rows = resp.data.get('results') if isinstance(resp.data, dict) else resp.data
        self.assertEqual(len(rows or []), 0,
                         "org A read org B's custom field values (IDOR)")

    def test_external_wait_provider_actions_do_not_bypass_foreign_item_scope(self):
        self.client.force_authenticate(self.user_a)
        for action, payload in (
            ('accept-external-wait', {'handoff_token': 'opaque'}),
            ('release-external-wait', {}),
        ):
            with self.subTest(action=action):
                response = self.client.post(
                    f'/api/items/{self.item_b.id}/{action}/',
                    payload,
                    format='json',
                )
                self.assertIn(response.status_code, (403, 404))


@override_settings(PULSE_CROSS_ORG_EXTERNAL_WAIT_ENABLED=True)
class PLS733CrossOrgExternalWaitTests(APITestCase):
    """ADR-0064 capability binding without a foreign-item oracle."""

    def setUp(self):
        self.consumer_user = User.objects.create_user(
            username='wait-consumer', email='consumer@wait.test', password='x'
        )
        self.provider_user = User.objects.create_user(
            username='wait-provider', email='provider@wait.test', password='x'
        )
        self.consumer_project = Project.objects.create(
            name='Consumer org', key='WCA', org_id=7101,
            access_level='private', is_active=True,
        )
        self.provider_project = Project.objects.create(
            name='Provider org', key='WPR', org_id=7102,
            access_level='private', is_active=True,
        )
        self.consumer = Item.objects.create(
            project=self.consumer_project,
            mode='task', title='Local capability carrier', status='open',
        )
        self.provider = Item.objects.create(
            project=self.provider_project,
            mode='task', title='Foreign authoritative delivery', status='open',
        )
        self.auth = patch('tasks.services.external_wait._authorize_item')
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def _prepare(self, item=None, policy=ExternalDependencyBinding.POLICY_TERMINAL_OR_READY):
        return prepare_cross_org_wait(
            item or self.consumer,
            user=self.consumer_user,
            release_policy=policy,
            local_display_reference='capability delivery',
        )

    def test_token_is_one_use_tuple_free_and_generation_bound(self):
        prepared = self._prepare()
        with self.assertRaisesMessage(ValidationError, 'external_wait_not_accepted'):
            accept_cross_org_wait(
                self.provider, user=self.provider_user, handoff_token='raw-task-key'
            )
        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.status, 'todo')

        accepted = accept_cross_org_wait(
            self.provider,
            user=self.provider_user,
            handoff_token=prepared['handoff_token'],
        )
        self.assertEqual(set(accepted), {'receipt', 'accepted'})
        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.status, 'todo')
        self.assertNotEqual(self.consumer.status, 'waiting_external')
        self.assertIsNone(self.consumer.resolution_id)
        self.assertFalse(
            ExternalWaitAuthority.objects.get(item=self.consumer).evidence.get(
                'provider_item_id'
            )
        )
        with self.assertRaisesMessage(ValidationError, 'external_wait_not_accepted'):
            accept_cross_org_wait(
                self.provider,
                user=self.provider_user,
                handoff_token=prepared['handoff_token'],
            )

        consumer_wait = ItemSerializer(self.consumer).data['external_wait']
        self.assertEqual(
            consumer_wait['current']['bindings'][0]['local_display_reference'],
            'capability delivery',
        )
        provider_wait = ItemSerializer(self.provider).data['external_wait']
        self.assertEqual(len(provider_wait['outbound_bindings']), 1)
        # Both sides receive opaque receipts/policy only. Foreign task/org
        # tuples never enter the normal item serializer.
        self.assertNotIn('provider_item', consumer_wait['current']['bindings'][0])
        self.assertNotIn('consumer_item', provider_wait['outbound_bindings'][0])

        stale = Item.objects.create(
            project=self.consumer_project, mode='task', title='Stale carrier',
            status='open',
        )
        stale_token = self._prepare(stale)
        stale.priority = 'urgent'
        stale.save(update_fields=['priority'])
        with self.assertRaisesMessage(ValidationError, 'external_wait_not_accepted'):
            accept_cross_org_wait(
                self.provider,
                user=self.provider_user,
                handoff_token=stale_token['handoff_token'],
            )
        stale.refresh_from_db()
        self.assertEqual(stale.status, 'todo')

    def test_production_order_terminal_signal_excludes_twice_then_resumes_once(self):
        prepared = self._prepare(
            policy=ExternalDependencyBinding.POLICY_TERMINAL
        )
        accept_cross_org_wait(
            self.provider,
            user=self.provider_user,
            handoff_token=prepared['handoff_token'],
        )
        for _ in range(2):
            self.assertTrue(
                filter_active_queue_items(
                    Item.objects.filter(pk=self.consumer.pk)
                ).exists()
            )

        self.provider.status = 'deferred'
        self.provider.save(update_fields=['status'])
        self.provider.priority = 'urgent'
        self.provider.save(update_fields=['priority'])
        self.assertEqual(
            ExternalDependencyReleaseEvent.objects.filter(
                binding__provider_item=self.provider
            ).count(),
            1,
        )
        event = ExternalDependencyReleaseEvent.objects.get(
            binding__provider_item=self.provider
        )
        self.assertEqual(process_release_event(event.id), 'applied')
        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.status, 'todo')
        self.assertTrue(
            filter_active_queue_items(
                Item.objects.filter(pk=self.consumer.pk)
            ).exists()
        )
        self.provider.save(update_fields=['status'])
        self.assertEqual(
            ExternalDependencyReleaseEvent.objects.filter(
                binding__provider_item=self.provider
            ).count(),
            1,
        )

    def test_pls903_kill_switch_fences_admission_but_drains_active_binding(self):
        """Rollback stops new cross-org waits without stranding an active one."""
        prepared = self._prepare(
            policy=ExternalDependencyBinding.POLICY_TERMINAL
        )
        accept_cross_org_wait(
            self.provider,
            user=self.provider_user,
            handoff_token=prepared['handoff_token'],
        )
        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.status, 'todo')
        self.assertNotEqual(self.consumer.status, 'waiting_external')

        fresh_consumer = Item.objects.create(
            project=self.consumer_project,
            mode='task',
            title='Admission attempted after rollback',
            status='open',
        )
        with override_settings(PULSE_CROSS_ORG_EXTERNAL_WAIT_ENABLED=False):
            with self.assertRaisesMessage(
                ValidationError, 'cross_org_external_wait_disabled'
            ):
                prepare_cross_org_wait(
                    fresh_consumer,
                    user=self.consumer_user,
                    release_policy=ExternalDependencyBinding.POLICY_TERMINAL,
                )

            # Provider terminality is the source-authoritative release signal.
            # Neither signal creation nor reduction may consult the admission
            # kill switch: active bindings must drain during rollback.
            self.provider.status = 'deferred'
            self.provider.save(update_fields=['status'])
            event = ExternalDependencyReleaseEvent.objects.get(
                binding__provider_item=self.provider
            )
            self.assertEqual(process_release_event(event.id), 'applied')

        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.status, 'todo')
        binding = ExternalDependencyBinding.objects.get(
            receipt_id=prepared['receipt']
        )
        self.assertEqual(binding.state, ExternalDependencyBinding.STATE_RELEASED)

    def test_expired_token_and_escalation_child_provider_refuse_identically(self):
        expired = self._prepare()
        ExternalDependencyBinding.objects.filter(
            receipt_id=expired['receipt']
        ).update(token_expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaisesMessage(ValidationError, 'external_wait_not_accepted'):
            accept_cross_org_wait(
                self.provider,
                user=self.provider_user,
                handoff_token=expired['handoff_token'],
            )

        second = self._prepare()
        escalation_provider = Item.objects.create(
            project=self.provider_project,
            mode='task', title='Escalation child', status='open',
            tags='blocker,decision-required,escalation,PLAT-4215',
        )
        with self.assertRaisesMessage(ValidationError, 'external_wait_not_accepted'):
            accept_cross_org_wait(
                escalation_provider,
                user=self.provider_user,
                handoff_token=second['handoff_token'],
            )
        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.status, 'todo')

    def test_multiple_holds_release_once_and_late_release_cannot_overwrite_revoke(self):
        first = self._prepare()
        accept_cross_org_wait(
            self.provider,
            user=self.provider_user,
            handoff_token=first['handoff_token'],
        )
        provider_three_project = Project.objects.create(
            name='Second provider org', key='WP3', org_id=7103,
            access_level='private', is_active=True,
        )
        provider_three = Item.objects.create(
            project=provider_three_project,
            mode='task', title='Second authoritative delivery', status='open',
        )
        second = self._prepare()
        accept_cross_org_wait(
            provider_three,
            user=self.provider_user,
            handoff_token=second['handoff_token'],
        )

        self.assertEqual(
            record_provider_release(
                self.provider,
                kind=ExternalDependencyReleaseEvent.KIND_EXPLICIT,
            ),
            1,
        )
        event_one = ExternalDependencyReleaseEvent.objects.get(
            binding__provider_item=self.provider
        )
        self.assertEqual(process_release_event(event_one.id), 'applied')
        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.status, 'todo')
        self.assertNotEqual(self.consumer.status, 'waiting_external')

        record_provider_release(
            provider_three,
            kind=ExternalDependencyReleaseEvent.KIND_EXPLICIT,
        )
        event_two = ExternalDependencyReleaseEvent.objects.get(
            binding__provider_item=provider_three
        )
        self.assertEqual(process_release_event(event_two.id), 'applied')
        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.status, 'todo')
        self.assertEqual(process_release_event(event_two.id), 'applied')

        next_wait = self._prepare()
        accept_cross_org_wait(
            self.provider,
            user=self.provider_user,
            handoff_token=next_wait['handoff_token'],
        )
        record_provider_release(
            self.provider,
            kind=ExternalDependencyReleaseEvent.KIND_EXPLICIT,
        )
        late_event = ExternalDependencyReleaseEvent.objects.filter(
            binding__provider_item=self.provider,
            state=ExternalDependencyReleaseEvent.STATE_PENDING,
        ).latest('created_at')
        revoke_cross_org_wait(
            self.consumer,
            user=self.consumer_user,
            reason='Local carrier no longer needs the dependency.',
        )
        self.assertEqual(process_release_event(late_event.id), 'stale')
        self.consumer.refresh_from_db()
        self.assertEqual(self.consumer.status, 'todo')

    def test_allowlisted_migration_is_dry_run_first_and_idempotent(self):
        consumer_project = Project.objects.create(
            name='Known carrier org', key='BKSH', org_id=7201,
            access_level='private', is_active=True,
        )
        provider_project = Project.objects.create(
            name='Known provider org', key='PLAT', org_id=7202,
            access_level='private', is_active=True,
        )
        consumer = Item.objects.create(
            project=consumer_project, item_key='BKSH-16', mode='task',
            title='Known migration carrier', status='open',
        )
        provider = Item.objects.create(
            project=provider_project, item_key='PLAT-5503', mode='task',
            title='Known migration provider', status='open',
        )
        common = {
            'consumer_org_id': 7201,
            'provider_org_id': 7202,
        }
        out = StringIO()
        call_command('migrate_external_wait_binding', stdout=out, **common)
        self.assertIn('DRY RUN', out.getvalue())
        consumer.refresh_from_db()
        self.assertNotEqual(consumer.status, 'waiting_external')
        self.assertFalse(
            ExternalDependencyBinding.objects.filter(
                consumer_item=consumer, provider_item=provider,
            ).exists()
        )

        call_command('migrate_external_wait_binding', apply=True, **common)
        call_command('migrate_external_wait_binding', apply=True, **common)
        consumer.refresh_from_db()
        self.assertNotEqual(consumer.status, 'waiting_external')
        self.assertEqual(
            ExternalDependencyBinding.objects.filter(
                consumer_item=consumer, provider_item=provider,
            ).count(),
            1,
        )
