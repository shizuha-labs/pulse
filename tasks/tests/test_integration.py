"""
Integration tests for Shizuha Hub.
Tests end-to-end workflows combining multiple components.
"""
from django.test import TestCase, TransactionTestCase, override_settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from datetime import timedelta
from unittest.mock import patch, MagicMock
import json

from tasks.models import (
    Item, Project, ApprovalRule, SilenceRule, EscalationRule,
    InhibitionRule, Subscription, FingerprintConfig, WebhookKey, TeamRoster
)
from tasks.services.deduplication import AlertDeduplicationService
from tasks.services.silencing import SilencingService
from tasks.services.escalation import EscalationService
from tasks.services.inhibition import InhibitionService
from .base import AuthenticatedAPITestCase

User = get_user_model()


class AuthenticatedTransactionTestCase(TransactionTestCase):
    """TransactionTestCase with authenticated staff user."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='testuser',
            email='testuser@example.com',
            password='testpass123',
            is_staff=True,
        )
        self.client.force_authenticate(user=self.user)


# =============================================================================
# Alertmanager Webhook Integration Tests
# =============================================================================

# PLS-86: ingestion is now async (the webhook enqueues process_alertmanager_alerts
# and returns immediately). Run the task eagerly so these end-to-end tests still
# exercise the dedup/create/resolve/truncate logic through the real (now-async) path.
# Persist=True: these tests assert Item.source_url / resolve behaviour of the
# legacy storage path. Default prod is DM-only (PULSE_ALERTMANAGER_PERSIST_ITEMS=false);
# that path is covered by test_alertmanager_dm_only.py.
@override_settings(
    CELERY_TASK_ALWAYS_EAGER=True,
    CELERY_TASK_EAGER_PROPAGATES=True,
    PULSE_ALERTMANAGER_PERSIST_ITEMS=True,
)
class AlertmanagerWebhookTests(TransactionTestCase):
    """Test Prometheus Alertmanager webhook ingestion."""

    def setUp(self):
        self.client = APIClient()
        self.project = Project.objects.create(
            org_id=1,
            name='Alertmanager Integration',
            key='ALRT',
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='architecture',
            defaults={
                'tiers': [['architecture@shizuha.com']],
                'is_active': True,
                'updated_by': 'test',
            },
        )
        self.webhook_key = WebhookKey.objects.create(
            user_id=1,
            user_email='ops@example.com',
            name='Prometheus',
            key='test-alertmanager-key',
            integration_type='prometheus',
            source_name='prometheus-test',
            default_severity='warning',
            default_project=self.project,
        )

    @patch('tasks.tasks.send_alert_notification.delay')
    def test_alertmanager_accepts_long_generator_url(self, mock_send_notification):
        """Long Prometheus generatorURL values must not 500 on Item.source_url."""
        long_generator_url = (
            'https://prometheus.example.com/graph?' + ('g0.expr=' + 'A' * 260)
        )
        self.assertGreater(len(long_generator_url), 200)

        payload = {
            'version': '4',
            'groupKey': '{}:{alertname="LongGeneratorURL"}',
            'status': 'firing',
            'receiver': 'pulse-alertmanager',
            'alerts': [
                {
                    'status': 'firing',
                    'labels': {
                        'alertname': 'LongGeneratorURL',
                        'severity': 'warning',
                        'namespace': 'monitoring',
                    },
                    'annotations': {
                        'summary': 'Long generatorURL regression test',
                    },
                    'startsAt': '2026-06-04T20:00:00Z',
                    'endsAt': '0001-01-01T00:00:00Z',
                    'generatorURL': long_generator_url,
                    'fingerprint': 'long-generator-url-fingerprint',
                }
            ],
        }

        response = self.client.post(
            f'/api/webhooks/alertmanager/{self.webhook_key.key}/',
            payload,
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['queued'], 1)  # PLS-86: webhook now enqueues
        item = Item.objects.get(source_id='long-generator-url-fingerprint')
        self.assertEqual(item.source_url, long_generator_url)
        self.assertGreater(len(item.source_url), 200)
        mock_send_notification.assert_called_once_with(item.id)

    @patch('tasks.tasks.send_alert_notification.delay')
    def test_alertmanager_truncates_oversized_generator_url(self, mock_send_notification):
        """Oversized generatorURL values are preserved in payload and truncated for source_url."""
        oversized_generator_url = (
            'https://prometheus.example.com/graph?' + ('g0.expr=' + 'B' * 2200)
        )
        self.assertGreater(len(oversized_generator_url), 2048)

        payload = {
            'version': '4',
            'groupKey': '{}:{alertname="OversizedGeneratorURL"}',
            'status': 'firing',
            'receiver': 'pulse-alertmanager',
            'alerts': [
                {
                    'status': 'firing',
                    'labels': {
                        'alertname': 'OversizedGeneratorURL',
                        'severity': 'warning',
                        'namespace': 'monitoring',
                    },
                    'annotations': {
                        'summary': 'Oversized generatorURL regression test',
                    },
                    'startsAt': '2026-06-04T20:00:00Z',
                    'endsAt': '0001-01-01T00:00:00Z',
                    'generatorURL': oversized_generator_url,
                    'fingerprint': 'oversized-generator-url-fingerprint',
                }
            ],
        }

        response = self.client.post(
            f'/api/webhooks/alertmanager/{self.webhook_key.key}/',
            payload,
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['queued'], 1)  # PLS-86: webhook now enqueues
        item = Item.objects.get(source_id='oversized-generator-url-fingerprint')
        self.assertEqual(item.source_url, oversized_generator_url[:2048])
        self.assertEqual(item.payload['generator_url'], oversized_generator_url)
        mock_send_notification.assert_called_once_with(item.id)

    @patch('tasks.tasks.send_alert_notification.delay')
    def test_alertmanager_resolves_alert_when_only_group_status_is_resolved(self, mock_send_notification):
        """A resolved Alertmanager group must clear the matching Pulse alert.

        Some Alertmanager webhook payloads carry `status=resolved` at the group
        level while individual alert objects omit `status`. That must resolve by
        fingerprint instead of falling through to the firing/dedup path.
        """
        fingerprint = 'resolved-group-status-fingerprint'
        Item.objects.create(
            mode='alert',
            item_type='alert.warning',
            title='NodeDiskPressure',
            description='node pressure',
            source=self.webhook_key.source_name,
            source_id=fingerprint,
            status='firing',
            severity='warning',
            priority='normal',
        )

        payload = {
            'version': '4',
            'groupKey': '{}:{alertname="NodeDiskPressure"}',
            'status': 'resolved',
            'receiver': 'pulse-alertmanager',
            'alerts': [
                {
                    # Intentionally no per-alert `status`.
                    'labels': {
                        'alertname': 'NodeDiskPressure',
                        'severity': 'warning',
                    },
                    'annotations': {
                        'summary': 'Node recovered',
                    },
                    'startsAt': '2026-06-21T00:00:00Z',
                    'endsAt': '2026-06-21T00:05:00Z',
                    'fingerprint': fingerprint,
                }
            ],
        }

        response = self.client.post(
            f'/api/webhooks/alertmanager/{self.webhook_key.key}/',
            payload,
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['queued'], 1)  # PLS-86: webhook now enqueues
        # Resolve effect verified via DB (the eager task did the work).
        item = Item.objects.get(source_id=fingerprint)
        self.assertEqual(item.status, 'resolved')
        self.assertEqual(item.resolved_by, f'alertmanager:{self.webhook_key.source_name}')
        mock_send_notification.assert_not_called()

    @patch('tasks.tasks.send_alert_notification.delay')
    def test_alertmanager_resolved_event_resolves_all_matching_duplicate_rows(self, mock_send_notification):
        """PLS-219: resolved webhooks must clear historical duplicate active rows, not only .first()."""
        fingerprint = 'duplicate-resolved-fingerprint'
        for idx, status_value in enumerate(['firing', 'acknowledged']):
            Item.objects.create(
                mode='alert',
                item_type='alert.warning',
                title=f'Duplicate NodeDown {idx}',
                description='duplicate active row',
                source=self.webhook_key.source_name,
                source_id=fingerprint,
                status=status_value,
                severity='warning',
                priority='normal',
            )

        payload = {
            'version': '4',
            'groupKey': '{}:{alertname="NodeDown"}',
            'status': 'resolved',
            'receiver': 'pulse-alertmanager',
            'alerts': [{
                'labels': {'alertname': 'NodeDown', 'severity': 'warning'},
                'annotations': {'summary': 'Node recovered'},
                'startsAt': '2026-06-21T00:00:00Z',
                'endsAt': '2026-06-21T00:05:00Z',
                'fingerprint': fingerprint,
            }],
        }

        response = self.client.post(
            f'/api/webhooks/alertmanager/{self.webhook_key.key}/',
            payload,
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['queued'], 1)
        self.assertEqual(
            Item.objects.filter(source_id=fingerprint, status__in=['firing', 'acknowledged']).count(),
            0,
        )
        self.assertEqual(Item.objects.filter(source_id=fingerprint, status='resolved').count(), 2)
        self.assertEqual(
            set(Item.objects.filter(source_id=fingerprint).values_list('resolved_by', flat=True)),
            {f'alertmanager:{self.webhook_key.source_name}'},
        )
        mock_send_notification.assert_not_called()

    @patch('tasks.tasks.send_alert_notification.delay')
    def test_owner_group_label_overrides_webhook_key_default_assignment_group(self, mock_send_notification):
        """PLS-122: owner_group label must route the alert to the labeled team, not the webhook-key default."""
        TeamRoster.objects.update_or_create(
            team_name='devops',
            defaults={
                'tiers': [['ichi@shizuha.com']],
                'is_active': True,
                'updated_by': 'test',
            },
        )
        self.webhook_key.default_assignment_group = 'architecture'
        self.webhook_key.save()

        payload = {
            'version': '4',
            'status': 'firing',
            'receiver': 'pulse-alertmanager',
            'alerts': [{
                'status': 'firing',
                'labels': {
                    'alertname': 'CortexLowPrefixCacheHit',
                    'severity': 'warning',
                    'owner_group': 'devops',
                },
                'annotations': {'summary': 'Cortex prefix cache below threshold'},
                'fingerprint': 'cortex-low-prefix-cache-fp',
            }],
        }
        response = self.client.post(
            f'/api/webhooks/alertmanager/{self.webhook_key.key}/',
            payload,
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        item = Item.objects.get(source_id='cortex-low-prefix-cache-fp')
        # Must be routed to the labeled team (canonical infra team is 'devops'
        # after the platform-ops -> devops rename), NOT architecture (from
        # webhook key default).
        self.assertEqual(item.assignment_group, 'devops')

    @patch('tasks.tasks.send_alert_notification.delay')
    def test_unknown_owner_group_label_falls_back_to_webhook_key_default_assignment_group(self, mock_send_notification):
        """PLS-122: unknown owner_group labels must not create phantom assignment groups."""
        self.webhook_key.default_assignment_group = 'architecture'
        self.webhook_key.save()

        payload = {
            'version': '4',
            'status': 'firing',
            'receiver': 'pulse-alertmanager',
            'alerts': [{
                'status': 'firing',
                'labels': {
                    'alertname': 'UnknownOwnerGroup',
                    'severity': 'warning',
                    'owner_group': 'not-a-real-team',
                },
                'annotations': {'summary': 'Unknown team should fall back'},
                'fingerprint': 'unknown-owner-group-fp',
            }],
        }
        response = self.client.post(
            f'/api/webhooks/alertmanager/{self.webhook_key.key}/',
            payload,
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        item = Item.objects.get(source_id='unknown-owner-group-fp')
        self.assertEqual(item.assignment_group, 'architecture')

    @patch('tasks.tasks.send_alert_notification.delay')
    def test_missing_owner_group_label_falls_back_to_webhook_key_default_assignment_group(self, mock_send_notification):
        """PLS-122: alerts without owner_group keep the webhook-key default route."""
        self.webhook_key.default_assignment_group = 'architecture'
        self.webhook_key.save()

        payload = {
            'version': '4',
            'status': 'firing',
            'receiver': 'pulse-alertmanager',
            'alerts': [{
                'status': 'firing',
                'labels': {
                    'alertname': 'MissingOwnerGroup',
                    'severity': 'warning',
                },
                'annotations': {'summary': 'Missing owner_group should fall back'},
                'fingerprint': 'missing-owner-group-fp',
            }],
        }
        response = self.client.post(
            f'/api/webhooks/alertmanager/{self.webhook_key.key}/',
            payload,
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        item = Item.objects.get(source_id='missing-owner-group-fp')
        self.assertEqual(item.assignment_group, 'architecture')

    @patch('tasks.tasks.send_alert_notification.delay')
    def test_owner_group_label_routes_to_canonical_team_slug(self, mock_send_notification):
        """PLS-122: label casing/spacing should not create non-canonical team slugs."""
        TeamRoster.objects.update_or_create(
            team_name='devops',
            defaults={
                'tiers': [['ichi@shizuha.com']],
                'is_active': True,
                'updated_by': 'test',
            },
        )
        self.webhook_key.default_assignment_group = 'architecture'
        self.webhook_key.save()

        payload = {
            'version': '4',
            'status': 'firing',
            'receiver': 'pulse-alertmanager',
            'alerts': [{
                'status': 'firing',
                'labels': {
                    'alertname': 'CanonicalOwnerGroup',
                    'severity': 'warning',
                    'owner_group': ' DevOps ',
                },
                'annotations': {'summary': 'Owner group should canonicalize'},
                'fingerprint': 'canonical-owner-group-fp',
            }],
        }
        response = self.client.post(
            f'/api/webhooks/alertmanager/{self.webhook_key.key}/',
            payload,
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        item = Item.objects.get(source_id='canonical-owner-group-fp')
        # ' DevOps ' normalizes to the canonical infra team 'devops'.
        self.assertEqual(item.assignment_group, 'devops')

    @patch('tasks.tasks.process_alertmanager_alerts.delay')
    def test_webhook_enqueues_and_returns_fast_without_sync_processing(self, mock_delay):
        """PLS-86: the webhook must NOT process the batch synchronously — it
        validates + enqueues + returns 200 immediately, so a burst (with retries)
        can't block the HTTP path on per-fingerprint SELECT…FOR UPDATE locks (the
        25-63s latency / 500 / dropped-batch cause)."""
        payload = {
            'version': '4',
            'status': 'firing',
            'receiver': 'pulse-alertmanager',
            'alerts': [{
                'status': 'firing',
                'labels': {'alertname': 'AsyncIngestTest', 'severity': 'warning'},
                'annotations': {'summary': 'async ingest'},
                'fingerprint': 'async-ingest-fingerprint',
            }],
        }
        response = self.client.post(
            f'/api/webhooks/alertmanager/{self.webhook_key.key}/', payload, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['queued'], 1)
        # Enqueued exactly once with (webhook_key.id, alerts, payload_status) — not inline.
        mock_delay.assert_called_once()
        args = mock_delay.call_args.args
        self.assertEqual(args[0], self.webhook_key.id)
        self.assertEqual(len(args[1]), 1)
        self.assertEqual(args[1][0]['fingerprint'], 'async-ingest-fingerprint')
        # The HTTP request created nothing synchronously (the worker does the upsert).
        self.assertFalse(Item.objects.filter(source_id='async-ingest-fingerprint').exists())

    @patch('tasks.tasks.send_alert_notification.delay')
    def test_source_id_less_resolved_alert_matches_computed_fingerprint_only(self, mock_send_notification):
        """PLS-293: resolving one source-id-less k3s alert must not resolve all source_id=NULL alerts."""
        self.webhook_key.source_name = 'k3s-alertmanager'
        self.webhook_key.save()

        firing_payload = {
            'version': '4',
            'status': 'firing',
            'receiver': 'pulse-alertmanager',
            'alerts': [
                {
                    'status': 'firing',
                    'labels': {
                        'alertname': 'KubeJobFailed',
                        'namespace': 'origin',
                        'job_name': 'build-one',
                        'severity': 'warning',
                    },
                    'annotations': {'summary': 'first job failed'},
                },
                {
                    'status': 'firing',
                    'labels': {
                        'alertname': 'KubeJobFailed',
                        'namespace': 'origin',
                        'job_name': 'build-two',
                        'severity': 'warning',
                    },
                    'annotations': {'summary': 'second job failed'},
                },
            ],
        }
        response = self.client.post(
            f'/api/webhooks/alertmanager/{self.webhook_key.key}/',
            firing_payload,
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        alerts = list(Item.objects.filter(source='k3s-alertmanager', source_id__isnull=True).order_by('title', 'id'))
        self.assertEqual(len(alerts), 2)
        self.assertEqual({a.status for a in alerts}, {'firing'})

        resolved_payload = {
            'version': '4',
            'status': 'resolved',
            'receiver': 'pulse-alertmanager',
            'alerts': [
                {
                    'status': 'resolved',
                    'labels': {
                        'alertname': 'KubeJobFailed',
                        'namespace': 'origin',
                        'job_name': 'build-one',
                        'severity': 'warning',
                    },
                    'annotations': {'summary': 'first job recovered'},
                },
            ],
        }
        response = self.client.post(
            f'/api/webhooks/alertmanager/{self.webhook_key.key}/',
            resolved_payload,
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        first = Item.objects.get(labels__job_name='build-one')
        second = Item.objects.get(labels__job_name='build-two')
        self.assertEqual(first.status, 'resolved')
        self.assertEqual(second.status, 'firing')


# =============================================================================
# Alert Lifecycle Integration Tests
# =============================================================================

class AlertLifecycleTests(AuthenticatedTransactionTestCase):
    """Test complete alert lifecycle from submission to resolution."""

    def test_full_alert_lifecycle(self):
        """Test complete alert lifecycle: submit -> acknowledge -> resolve."""
        # 1. Submit alert (AllowAny endpoint)
        alert_data = {
            'title': 'High CPU Usage on web-01',
            'source': 'prometheus',
            'severity': 'warning',
            'labels': {'host': 'web-01', 'service': 'nginx'}
        }

        submit_response = self.client.post('/api/alerts/', alert_data, format='json')
        self.assertEqual(submit_response.status_code, status.HTTP_201_CREATED)

        alert_id = submit_response.data['item_id']

        # Verify initial state
        alert = Item.objects.get(id=alert_id)
        self.assertEqual(alert.status, 'firing')
        self.assertEqual(alert.occurrence_count, 1)

        # 2. Duplicate alert comes in
        submit_response2 = self.client.post('/api/alerts/', alert_data, format='json')
        self.assertEqual(submit_response2.status_code, status.HTTP_200_OK)
        self.assertEqual(submit_response2.data['occurrence_count'], 2)

        # 3. Acknowledge alert (requires auth)
        ack_response = self.client.post(
            f'/api/items/{alert_id}/acknowledge/',
            {'comment': 'Looking into it'},
            format='json'
        )
        self.assertEqual(ack_response.status_code, status.HTTP_200_OK)

        alert.refresh_from_db()
        self.assertEqual(alert.status, 'acknowledged')
        self.assertIsNotNone(alert.acknowledged_at)

        # 4. Resolve alert
        resolve_response = self.client.post(
            f'/api/items/{alert_id}/resolve/',
            {'resolution_note': 'Scaled up the servers'},
            format='json'
        )
        self.assertEqual(resolve_response.status_code, status.HTTP_200_OK)

        alert.refresh_from_db()
        self.assertEqual(alert.status, 'resolved')
        self.assertIsNotNone(alert.resolved_at)
        self.assertEqual(alert.resolution_note, 'Scaled up the servers')

    def test_alert_silence_workflow(self):
        """Test silencing an alert during investigation."""
        # Submit alert
        alert_data = {
            'title': 'Noisy Alert',
            'source': 'test',
            'source_id': 'noisy-alert',
            'severity': 'info',
            'labels': {}
        }

        submit_response = self.client.post('/api/alerts/', alert_data, format='json')
        alert_id = submit_response.data['item_id']

        # Silence for 30 minutes (requires auth)
        silence_response = self.client.post(
            f'/api/items/{alert_id}/silence/',
            {'duration_minutes': 30, 'reason': 'Known issue, investigating'},
            format='json'
        )
        self.assertEqual(silence_response.status_code, status.HTTP_200_OK)

        # Verify silenced
        alert = Item.objects.get(id=alert_id)
        self.assertTrue(alert.is_silenced())

        # Submitting same alert should still deduplicate but stay silenced
        submit_response2 = self.client.post('/api/alerts/', alert_data, format='json')
        self.assertEqual(submit_response2.data['item_id'], alert_id)

    def test_alert_with_recovery(self):
        """Test alert resolution via recovery signal."""
        # Submit alert with correlation_id
        alert_data = {
            'title': 'Database Connection Error',
            'source': 'healthcheck',
            'severity': 'critical',
            'correlation_id': 'db-health-check-001',
            'labels': {'db': 'postgres-main'}
        }

        submit_response = self.client.post('/api/alerts/', alert_data, format='json')
        alert_id = submit_response.data['item_id']

        # Simulate recovery
        resolved = AlertDeduplicationService.handle_recovery({
            'source': 'healthcheck',
            'correlation_id': 'db-health-check-001'
        })

        self.assertEqual(len(resolved), 1)
        alert = Item.objects.get(id=alert_id)
        self.assertEqual(alert.status, 'resolved')
        self.assertIn('recovery', alert.resolved_by)


# =============================================================================
# Action Approval Workflow Tests
# =============================================================================

class ActionApprovalWorkflowTests(AuthenticatedAPITestCase):
    """Test action submission and approval workflows."""

    def test_action_approval_workflow(self):
        """Test complete action approval workflow."""
        # 1. Submit action requiring approval (AllowAny endpoint)
        action_data = {
            'title': 'Execute Hedging Trade',
            'source': 'arbiter-backend',
            'item_type': 'action.hedging.short',
            'payload': {'symbol': 'BTCUSDT', 'side': 'sell', 'quantity': 0.05},
            'requires_approval': True,
            'callback_url': 'https://example.com/execute'
        }

        submit_response = self.client.post('/api/actions/submit/', action_data, format='json')
        self.assertEqual(submit_response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(submit_response.data['requires_approval'])

        action_id = submit_response.data['item_id']

        # Verify pending state
        action = Item.objects.get(id=action_id)
        self.assertEqual(action.status, 'pending')
        self.assertTrue(action.requires_approval)

        # 2. Approve action (requires auth)
        approve_response = self.client.post(
            f'/api/items/{action_id}/approve/',
            {'comment': 'Risk assessed, approved'},
            format='json'
        )
        self.assertEqual(approve_response.status_code, status.HTTP_200_OK)

        action.refresh_from_db()
        self.assertEqual(action.status, 'approved')
        self.assertIsNotNone(action.approved_at)

    def test_action_rejection_workflow(self):
        """Test action rejection workflow."""
        # Submit action
        action_data = {
            'title': 'Risky Action',
            'source': 'test',
            'requires_approval': True
        }

        submit_response = self.client.post('/api/actions/submit/', action_data, format='json')
        action_id = submit_response.data['item_id']

        # Reject action (requires auth)
        reject_response = self.client.post(
            f'/api/items/{action_id}/reject/',
            {'reason': 'Too risky during market volatility'},
            format='json'
        )
        self.assertEqual(reject_response.status_code, status.HTTP_200_OK)

        action = Item.objects.get(id=action_id)
        self.assertEqual(action.status, 'rejected')
        self.assertEqual(action.rejection_reason, 'Too risky during market volatility')

    def test_auto_approve_rule_workflow(self):
        """Test action auto-approval via approval rule."""
        # Create auto-approve rule
        ApprovalRule.objects.create(
            name='Auto-approve low risk trades',
            item_types=['action.trading.low_risk'],
            auto_approve=True,
            priority=10,
            is_active=True
        )

        rule = ApprovalRule.objects.first()
        self.assertTrue(rule.auto_approve)
        self.assertIn('action.trading.low_risk', rule.item_types)


# =============================================================================
# Silencing Integration Tests
# =============================================================================

class SilencingIntegrationTests(TransactionTestCase):
    """Test silencing integration across components."""

    def setUp(self):
        """Set up test client."""
        self.client = APIClient()

    def test_silence_rule_affects_alerts(self):
        """Test that silence rules affect new alerts."""
        SilenceRule.objects.create(
            name='Silence Prometheus Info',
            sources=['prometheus'],
            severities=['info'],
            is_active=True,
            reason='Noisy during deployment'
        )

        alert_data = {
            'title': 'Info Alert',
            'source': 'prometheus',
            'source_id': 'info-alert',
            'severity': 'info',
            'labels': {}
        }

        response = self.client.post('/api/alerts/', alert_data, format='json')
        alert_id = response.data['item_id']

        alert = Item.objects.get(id=alert_id)
        self.assertTrue(SilencingService.is_silenced(alert))

    def test_silence_rule_with_label_matchers(self):
        """Test silence rule with label matching."""
        SilenceRule.objects.create(
            name='Silence Dev Environment',
            label_matchers={'env': 'development'},
            is_active=True
        )

        dev_alert = Item.objects.create(
            mode='alert',
            title='Dev Alert',
            labels={'env': 'development'}
        )
        self.assertTrue(SilencingService.is_silenced(dev_alert))

        prod_alert = Item.objects.create(
            mode='alert',
            title='Prod Alert',
            labels={'env': 'production'}
        )
        self.assertFalse(SilencingService.is_silenced(prod_alert))

    def test_timed_silence_rule(self):
        """Test silence rule with time bounds."""
        active_rule = SilenceRule.objects.create(
            name='Active Maintenance Window',
            sources=['all'],
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=1),
            is_active=True
        )

        self.assertTrue(active_rule.is_currently_active())

        expired_rule = SilenceRule.objects.create(
            name='Expired Maintenance',
            sources=['all'],
            starts_at=timezone.now() - timedelta(hours=3),
            ends_at=timezone.now() - timedelta(hours=1),
            is_active=True
        )

        self.assertFalse(expired_rule.is_currently_active())


# =============================================================================
# Escalation Integration Tests
# =============================================================================

class EscalationIntegrationTests(TestCase):
    """Test escalation integration across components."""

    def setUp(self):
        """Set up escalation rule."""
        self.rule = EscalationRule.objects.create(
            name='Critical Alert Escalation',
            modes=['alert'],
            min_severity='warning',
            stages=[
                {
                    'delay_minutes': 5,
                    'actions': [
                        {'type': 'bump_severity', 'to': 'error'},
                        {'type': 'add_channels', 'channels': ['slack']}
                    ]
                },
                {
                    'delay_minutes': 15,
                    'actions': [
                        {'type': 'bump_severity', 'to': 'critical'},
                        {'type': 'add_channels', 'channels': ['phone_call']},
                        {'type': 'set_priority', 'to': 'urgent'}
                    ]
                }
            ],
            is_active=True
        )

    def test_escalation_progression(self):
        """Test alert escalates through multiple stages."""
        alert = Item.objects.create(
            mode='alert',
            title='Unacknowledged Alert',
            severity='warning',
            status='firing',
            first_fired_at=timezone.now() - timedelta(minutes=10)
        )

        EscalationService.escalate_item(alert, self.rule)

        alert.refresh_from_db()
        self.assertEqual(alert.severity, 'error')
        self.assertEqual(alert.escalation_level, 1)
        self.assertIn('slack', alert.notification_preferences.get('escalation_channels', []))

        alert.last_escalated_at = timezone.now() - timedelta(minutes=20)
        alert.save()

        EscalationService.escalate_item(alert, self.rule)

        alert.refresh_from_db()
        self.assertEqual(alert.severity, 'critical')
        self.assertEqual(alert.escalation_level, 2)
        self.assertEqual(alert.priority, 'urgent')
        self.assertIn('phone_call', alert.notification_preferences.get('escalation_channels', []))

    def test_acknowledged_alert_stops_escalation(self):
        """Test that acknowledged alerts may have different escalation behavior."""
        alert = Item.objects.create(
            mode='alert',
            title='Acknowledged Alert',
            severity='warning',
            status='acknowledged',
            first_fired_at=timezone.now() - timedelta(minutes=30),
            acknowledged_at=timezone.now() - timedelta(minutes=25)
        )

        rule_matched = EscalationService.find_matching_rule(alert)
        self.assertIsNotNone(rule_matched)


# =============================================================================
# Inhibition Integration Tests
# =============================================================================

class InhibitionIntegrationTests(TestCase):
    """Test inhibition integration across components."""

    def setUp(self):
        """Set up inhibition rule."""
        self.rule = InhibitionRule.objects.create(
            name='Database Cascade Inhibition',
            source_item_types=['alert.database.*'],
            source_severities=['critical'],
            target_item_types=['alert.api.*', 'alert.application.*'],
            equal_labels=['cluster', 'env'],
            is_active=True
        )

    def test_cascade_inhibition(self):
        """Test cascading failure inhibition."""
        db_alert = Item.objects.create(
            mode='alert',
            title='Database Connection Failed',
            item_type='alert.database.connection',
            severity='critical',
            status='firing',
            labels={'cluster': 'main', 'env': 'production', 'db': 'postgres'}
        )

        api_alert = Item.objects.create(
            mode='alert',
            title='API Response Timeout',
            item_type='alert.api.timeout',
            status='firing',
            labels={'cluster': 'main', 'env': 'production', 'service': 'orders'}
        )

        app_alert = Item.objects.create(
            mode='alert',
            title='Application Error Rate High',
            item_type='alert.application.errors',
            status='firing',
            labels={'cluster': 'main', 'env': 'production', 'app': 'checkout'}
        )

        other_cluster_alert = Item.objects.create(
            mode='alert',
            title='Other Cluster API Error',
            item_type='alert.api.error',
            status='firing',
            labels={'cluster': 'secondary', 'env': 'production'}
        )

        self.assertTrue(InhibitionService.is_inhibited(api_alert))
        self.assertTrue(InhibitionService.is_inhibited(app_alert))
        self.assertFalse(InhibitionService.is_inhibited(other_cluster_alert))
        self.assertFalse(InhibitionService.is_inhibited(db_alert))

    def test_inhibition_resolves_when_source_resolves(self):
        """Test that inhibition lifts when source alert resolves."""
        db_alert = Item.objects.create(
            mode='alert',
            title='Database Down',
            item_type='alert.database.connection',
            severity='critical',
            status='resolved',
            labels={'cluster': 'main', 'env': 'production'}
        )

        api_alert = Item.objects.create(
            mode='alert',
            title='API Error',
            item_type='alert.api.error',
            status='firing',
            labels={'cluster': 'main', 'env': 'production'}
        )

        self.assertFalse(InhibitionService.is_inhibited(api_alert))


# =============================================================================
# Mixed Mode Workflow Tests
# =============================================================================

class MixedModeWorkflowTests(AuthenticatedAPITestCase):
    """Test workflows involving multiple item modes."""

    def test_alert_triggers_action(self):
        """Test scenario where alert triggers an action for remediation."""
        # 1. Alert comes in (AllowAny)
        alert_data = {
            'title': 'Disk Space Low',
            'source': 'monitoring',
            'severity': 'warning',
            'labels': {'host': 'server-01', 'disk': '/var/log'}
        }

        alert_response = self.client.post('/api/alerts/', alert_data, format='json')
        alert_id = alert_response.data['item_id']

        # 2. Automation submits remediation action (AllowAny)
        action_data = {
            'title': 'Clean Old Logs',
            'source': 'automation',
            'item_type': 'action.remediation.cleanup',
            'payload': {
                'triggered_by_alert': alert_id,
                'command': 'logrotate -f /etc/logrotate.conf',
                'host': 'server-01'
            },
            'requires_approval': True
        }

        action_response = self.client.post('/api/actions/submit/', action_data, format='json')
        self.assertEqual(action_response.status_code, status.HTTP_201_CREATED)
        action_id = action_response.data['item_id']

        # 3. Human approves action (requires auth)
        approve_response = self.client.post(
            f'/api/items/{action_id}/approve/',
            {'comment': 'Safe to clean logs'},
            format='json'
        )
        self.assertEqual(approve_response.status_code, status.HTTP_200_OK)

        # 4. After action completes, alert is resolved
        alert = Item.objects.get(id=alert_id)
        alert.resolve(resolved_by='automation', note='Cleaned up via automated action')
        self.assertEqual(alert.status, 'resolved')

    def test_unified_dashboard_view(self):
        """Test retrieving all item types in unified view."""
        Item.objects.create(
            mode='task', title='Code Review', priority='high',
            created_by_id=self.user.id
        )
        Item.objects.create(
            mode='action', title='Deploy Service', status='pending',
            created_by_id=self.user.id
        )
        Item.objects.create(
            mode='alert', title='High Latency', status='firing', severity='warning',
            created_by_id=self.user.id
        )

        response = self.client.get('/api/items/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 3)

        modes = {item['mode'] for item in response.data['results']}
        self.assertEqual(modes, {'task', 'action', 'alert'})

    def test_filtering_across_modes(self):
        """Test filtering works across all modes."""
        Item.objects.create(
            mode='task', title='Production Task',
            labels={'env': 'production'}, created_by_id=self.user.id
        )
        Item.objects.create(
            mode='alert', title='Production Alert',
            labels={'env': 'production'}, created_by_id=self.user.id
        )
        Item.objects.create(
            mode='task', title='Dev Task',
            labels={'env': 'development'}, created_by_id=self.user.id
        )

        response = self.client.get('/api/items/', {'mode': 'task'})
        self.assertEqual(response.data['count'], 2)


# =============================================================================
# Deduplication Edge Cases Tests
# =============================================================================

class DeduplicationEdgeCasesTests(TransactionTestCase):
    """Test edge cases in alert deduplication."""

    def test_rapid_duplicate_submission(self):
        """Test handling rapid duplicate submissions."""
        alert_data = {
            'title': 'Rapid Alert',
            'source': 'test',
            'severity': 'warning',
            'labels': {'key': 'value'}
        }

        results = []
        for i in range(5):
            alert, is_new = AlertDeduplicationService.submit_alert(alert_data)
            results.append((alert.id, is_new))

        alert_ids = {r[0] for r in results}
        self.assertEqual(len(alert_ids), 1)

        self.assertTrue(results[0][1])
        for i in range(1, 5):
            self.assertFalse(results[i][1])

        alert = Item.objects.get(id=results[0][0])
        self.assertEqual(alert.occurrence_count, 5)

    def test_different_sources_different_alerts(self):
        """Test same labels from different sources create different alerts."""
        labels = {'host': 'web-01'}

        data1 = {
            'title': 'Prometheus Alert',
            'source': 'prometheus',
            'severity': 'warning',
            'labels': labels
        }
        data2 = {
            'title': 'Grafana Alert',
            'source': 'grafana',
            'severity': 'warning',
            'labels': labels
        }

        alert1, _ = AlertDeduplicationService.submit_alert(data1)
        alert2, _ = AlertDeduplicationService.submit_alert(data2)

        self.assertNotEqual(alert1.id, alert2.id)

    def test_custom_fingerprint_config(self):
        """Test custom fingerprint config affects deduplication."""
        FingerprintConfig.objects.create(
            source='custom-source',
            label_fields=['alertname'],
            payload_fields=[]
        )

        data1 = {
            'title': 'Alert 1',
            'source': 'custom-source',
            'severity': 'warning',
            'labels': {'alertname': 'HighCPU', 'instance': 'web-01:9090'}
        }
        data2 = {
            'title': 'Alert 2',
            'source': 'custom-source',
            'severity': 'warning',
            'labels': {'alertname': 'HighCPU', 'instance': 'web-02:9090'}
        }

        alert1, is_new1 = AlertDeduplicationService.submit_alert(data1)
        alert2, is_new2 = AlertDeduplicationService.submit_alert(data2)

        self.assertEqual(alert1.id, alert2.id)


# =============================================================================
# Rule Interaction Tests
# =============================================================================

class RuleInteractionTests(TestCase):
    """Test interactions between different rule types."""

    def test_silenced_alert_not_escalated(self):
        """Test that silenced alerts should typically not be escalated."""
        EscalationRule.objects.create(
            name='Test Escalation',
            modes=['alert'],
            stages=[{'delay_minutes': 0, 'actions': [{'type': 'bump_severity', 'to': 'critical'}]}],
            is_active=True
        )

        alert = Item.objects.create(
            mode='alert',
            title='Silenced Alert',
            severity='warning',
            status='firing',
            silenced_until=timezone.now() + timedelta(hours=1),
            first_fired_at=timezone.now() - timedelta(minutes=30)
        )

        self.assertTrue(alert.is_silenced())
        self.assertTrue(SilencingService.is_silenced(alert))

    def test_multiple_inhibition_rules(self):
        """Test multiple inhibition rules are evaluated."""
        InhibitionRule.objects.create(
            name='DB -> API',
            source_item_types=['alert.database.*'],
            target_item_types=['alert.api.*'],
            equal_labels=['cluster'],
            is_active=True
        )

        InhibitionRule.objects.create(
            name='Network -> All',
            source_item_types=['alert.network.*'],
            source_severities=['critical'],
            target_item_types=['alert.*'],
            equal_labels=['datacenter'],
            is_active=True
        )

        network_alert = Item.objects.create(
            mode='alert',
            title='Network Outage',
            item_type='alert.network.connectivity',
            severity='critical',
            status='firing',
            labels={'datacenter': 'us-east-1'}
        )

        api_alert = Item.objects.create(
            mode='alert',
            title='API Error',
            item_type='alert.api.timeout',
            status='firing',
            labels={'datacenter': 'us-east-1'}
        )

        self.assertTrue(InhibitionService.is_inhibited(api_alert))


# =============================================================================
# API Error Handling Tests
# =============================================================================

class APIErrorHandlingTests(AuthenticatedAPITestCase):
    """Test API error handling."""

    def test_invalid_json_returns_400(self):
        """Test invalid JSON returns 400."""
        response = self.client.post(
            '/api/items/',
            'not valid json',
            content_type='application/json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_required_field_returns_400(self):
        """Test missing required field returns 400."""
        response = self.client.post(
            '/api/alerts/',
            {'source': 'test'},  # Missing title
            format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_severity_returns_400(self):
        """Test invalid severity returns 400."""
        response = self.client.post(
            '/api/alerts/',
            {
                'title': 'Test',
                'source': 'test',
                'severity': 'invalid_severity'
            },
            format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_action_on_nonexistent_item_returns_404(self):
        """Test action on nonexistent item returns 404."""
        response = self.client.post('/api/items/99999/acknowledge/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_bulk_operation_with_invalid_ids(self):
        """Test bulk operation handles invalid IDs gracefully."""
        response = self.client.post(
            '/api/alerts/bulk-acknowledge/',
            {'item_ids': [99999, 99998]},
            format='json'
        )

        # Should succeed but acknowledge 0 items
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)
