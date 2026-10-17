"""
Tests for Shizuha Hub services.
Tests deduplication, silencing, escalation, and inhibition services.
"""
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch, MagicMock

from tasks.models import (
    Item, ApprovalRule, SilenceRule, EscalationRule,
    InhibitionRule, FingerprintConfig
)
from tasks.services.deduplication import AlertDeduplicationService, AlertIdentityError
from tasks.services.silencing import SilencingService
from tasks.services.escalation import EscalationService
from tasks.services.inhibition import InhibitionService


# =============================================================================
# Alert Deduplication Service Tests
# =============================================================================

class AlertDeduplicationServiceTests(TransactionTestCase):
    """Test AlertDeduplicationService functionality."""

    def test_submit_new_alert_creates_item(self):
        """Test submitting a new alert creates an Item."""
        data = {
            'title': 'High CPU Usage',
            'source': 'prometheus',
            'item_type': 'alert.error',
            'severity': 'warning',
            'labels': {'host': 'web-01', 'job': 'nginx'}
        }

        alert, is_new = AlertDeduplicationService.submit_alert(data)

        self.assertTrue(is_new)
        self.assertEqual(alert.mode, 'alert')
        self.assertEqual(alert.title, 'High CPU Usage')
        self.assertEqual(alert.status, 'firing')
        self.assertIsNotNone(alert.fingerprint)
        self.assertEqual(alert.occurrence_count, 1)

    def test_submit_duplicate_alert_deduplicates(self):
        """Test submitting duplicate alert updates existing one."""
        data = {
            'title': 'High CPU Usage',
            'source': 'prometheus',
            'item_type': 'alert.error',
            'severity': 'warning',
            'labels': {'host': 'web-01'}
        }

        alert1, is_new1 = AlertDeduplicationService.submit_alert(data)
        alert2, is_new2 = AlertDeduplicationService.submit_alert(data)

        self.assertTrue(is_new1)
        self.assertFalse(is_new2)
        self.assertEqual(alert1.id, alert2.id)
        self.assertEqual(alert2.occurrence_count, 2)

    def test_different_labels_create_different_alerts(self):
        """Test different labels create different alerts."""
        data1 = {
            'title': 'High CPU',
            'source': 'prometheus',
            'severity': 'warning',
            'labels': {'host': 'web-01'}
        }
        data2 = {
            'title': 'High CPU',
            'source': 'prometheus',
            'severity': 'warning',
            'labels': {'host': 'web-02'}  # Different host
        }

        alert1, is_new1 = AlertDeduplicationService.submit_alert(data1)
        alert2, is_new2 = AlertDeduplicationService.submit_alert(data2)

        self.assertTrue(is_new1)
        self.assertTrue(is_new2)
        self.assertNotEqual(alert1.id, alert2.id)
        self.assertNotEqual(alert1.fingerprint, alert2.fingerprint)

    def test_source_id_dedups_despite_label_drift(self):
        """PLS-90: the SAME upstream source_id (Alertmanager fingerprint) dedups
        to ONE item even when labels differ between firings (which would compute
        different fingerprints) — the cause of 479 items for ~84 unique alerts."""
        base = {
            'title': 'CrossNodePodCIDRUnreachable',
            'source': 'k3s-alertmanager',
            'severity': 'warning',
            'source_id': 'amfp-deadbeef',
        }
        a1, new1 = AlertDeduplicationService.submit_alert({**base, 'labels': {'instance': '10.0.0.1'}})
        a2, new2 = AlertDeduplicationService.submit_alert({**base, 'labels': {'instance': '10.0.0.2'}})
        self.assertTrue(new1)
        self.assertFalse(new2)            # deduped on source_id despite label drift
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(a2.occurrence_count, 2)

    def test_source_id_refire_after_resolve_creates_new(self):
        """PLS-90 AC: re-firing a RESOLVED alert (same source_id) creates a NEW
        item — dedup only matches ACTIVE (firing/acknowledged) alerts."""
        data = {
            'title': 'KubePdbNotEnoughHealthyPods',
            'source': 'k3s-alertmanager',
            'severity': 'critical',
            'source_id': 'amfp-cafe',
            'labels': {},
        }
        a1, new1 = AlertDeduplicationService.submit_alert(data)
        a1.resolve(resolved_by='test')
        a2, new2 = AlertDeduplicationService.submit_alert(data)
        self.assertTrue(new1)
        self.assertTrue(new2)             # resolved item not matched → new item
        self.assertNotEqual(a1.id, a2.id)

    def test_submit_alert_updates_last_fired_at(self):
        """Test duplicate alert updates last_fired_at."""
        data = {
            'title': 'Alert',
            'source': 'test',
            'severity': 'info',
            'labels': {'instance': 'test-1'}
        }

        alert1, _ = AlertDeduplicationService.submit_alert(data)
        first_fired = alert1.first_fired_at
        last_fired_1 = alert1.last_fired_at

        # Wait a tiny bit and submit again
        import time
        time.sleep(0.01)

        alert2, is_new = AlertDeduplicationService.submit_alert(data)

        self.assertFalse(is_new)
        self.assertEqual(alert2.first_fired_at, first_fired)
        self.assertGreater(alert2.last_fired_at, last_fired_1)

    def test_k3s_alertmanager_without_source_id_uses_builtin_stable_identity(self):
        """PLS-293: k3s alerts without AM fingerprint dedupe on bounded labels."""
        base = {
            'title': 'KubeJobFailed',
            'source': 'k3s-alertmanager',
            'severity': 'warning',
            'labels': {
                'alertname': 'KubeJobFailed',
                'namespace': 'origin',
                'job_name': 'build-123',
                'pod': 'kube-state-metrics-a',
            },
        }

        first, new_first = AlertDeduplicationService.submit_alert(base)
        second, new_second = AlertDeduplicationService.submit_alert({
            **base,
            'labels': {
                **base['labels'],
                # This label is intentionally absent from the k3s builtin
                # config; exporter churn must not split the alert identity.
                'prometheus_replica': 'replica-b',
            },
        })

        self.assertTrue(new_first)
        self.assertFalse(new_second)
        self.assertEqual(first.id, second.id)
        self.assertIn('alertname', second.fingerprint_fields)
        self.assertIn('job_name', second.fingerprint_fields)

    def test_identityless_alert_is_rejected_without_creating_item(self):
        """PLS-293: no source_id and no configured fields is a dead-letter."""
        with self.assertRaises(AlertIdentityError):
            AlertDeduplicationService.submit_alert({
                'title': 'Identityless',
                'source': 'k3s-alertmanager',
                'severity': 'warning',
                'labels': {'prometheus_replica': 'replica-a'},
            })

        self.assertFalse(Item.objects.filter(title='Identityless').exists())

    def test_submit_alert_with_custom_fingerprint_config(self):
        """Test fingerprint computation with custom config."""
        FingerprintConfig.objects.create(
            source='custom-source',
            label_fields=['alertname', 'instance'],  # Only these fields
            payload_fields=[]
        )

        data = {
            'title': 'Custom Alert',
            'source': 'custom-source',
            'severity': 'warning',
            'labels': {
                'alertname': 'HighCPU',
                'instance': 'web-01:9090',
                'extra': 'ignored'  # Should be ignored in fingerprint
            }
        }

        alert, _ = AlertDeduplicationService.submit_alert(data)
        self.assertIsNotNone(alert.fingerprint)

    def test_compute_fingerprint_deterministic(self):
        """Test fingerprint computation is deterministic."""
        # compute_fingerprint takes item_type, source, labels as separate args
        fp1 = AlertDeduplicationService.compute_fingerprint(
            item_type='alert.test',
            source='test',
            labels={'a': '1', 'b': '2'}
        )
        fp2 = AlertDeduplicationService.compute_fingerprint(
            item_type='alert.test',
            source='test',
            labels={'a': '1', 'b': '2'}
        )

        self.assertEqual(fp1, fp2)

    def test_compute_fingerprint_order_independent(self):
        """Test fingerprint is independent of label order."""
        fp1 = AlertDeduplicationService.compute_fingerprint(
            item_type='alert.test',
            source='test',
            labels={'a': '1', 'b': '2', 'c': '3'}
        )
        fp2 = AlertDeduplicationService.compute_fingerprint(
            item_type='alert.test',
            source='test',
            labels={'c': '3', 'a': '1', 'b': '2'}  # Different order
        )

        self.assertEqual(fp1, fp2)

    def test_handle_recovery_resolves_alert(self):
        """Test recovery message resolves matching alerts."""
        # Create firing alert
        alert = Item.objects.create(
            mode='alert',
            title='Test Alert',
            status='firing',
            correlation_id='test-correlation-123',
            source='test'
        )

        # Handle recovery - takes a dict with correlation_id
        resolved = AlertDeduplicationService.handle_recovery({
            'source': 'test',
            'correlation_id': 'test-correlation-123'
        })

        self.assertEqual(len(resolved), 1)
        alert.refresh_from_db()
        self.assertEqual(alert.status, 'resolved')
        self.assertIn('recovery', alert.resolved_by)

    def test_handle_recovery_no_matching_alerts(self):
        """Test recovery with no matching alerts."""
        resolved = AlertDeduplicationService.handle_recovery({
            'source': 'test',
            'correlation_id': 'nonexistent-correlation'
        })
        self.assertEqual(len(resolved), 0)

    def test_resolved_alert_allows_new_with_same_fingerprint(self):
        """Test that resolved alert allows new alert with same fingerprint."""
        data = {
            'title': 'Alert',
            'source': 'test',
            'severity': 'warning',
            'labels': {'key': 'value'}
        }

        # Create and resolve first alert
        alert1, _ = AlertDeduplicationService.submit_alert(data)
        alert1.status = 'resolved'
        alert1.save()

        # Submit same alert again - should create new one
        alert2, is_new = AlertDeduplicationService.submit_alert(data)

        self.assertTrue(is_new)
        self.assertNotEqual(alert1.id, alert2.id)


# =============================================================================
# Silencing Service Tests
# =============================================================================

class SilencingServiceTests(TestCase):
    """Test SilencingService functionality."""

    def test_is_silenced_direct_silencing(self):
        """Test is_silenced with direct silencing on item."""
        item = Item.objects.create(
            title='Silenced Item',
            silenced_until=timezone.now() + timedelta(hours=1)
        )

        self.assertTrue(SilencingService.is_silenced(item))

    def test_is_silenced_expired_silencing(self):
        """Test is_silenced with expired direct silencing."""
        item = Item.objects.create(
            title='Was Silenced',
            silenced_until=timezone.now() - timedelta(hours=1)
        )

        self.assertFalse(SilencingService.is_silenced(item))

    def test_is_silenced_by_rule(self):
        """Test is_silenced with matching silence rule."""
        SilenceRule.objects.create(
            name='Silence Prometheus',
            sources=['prometheus'],
            is_active=True
        )

        item = Item.objects.create(
            title='Prometheus Alert',
            source='prometheus'
        )

        self.assertTrue(SilencingService.is_silenced(item))

    def test_is_silenced_no_matching_rule(self):
        """Test is_silenced with no matching rule."""
        SilenceRule.objects.create(
            name='Silence Prometheus',
            sources=['prometheus'],
            is_active=True
        )

        item = Item.objects.create(
            title='Grafana Alert',
            source='grafana'  # Different source
        )

        self.assertFalse(SilencingService.is_silenced(item))

    def test_find_matching_rule_by_mode(self):
        """Test finding silence rule by mode."""
        SilenceRule.objects.create(
            name='Silence Alerts',
            modes=['alert'],
            is_active=True
        )

        alert = Item.objects.create(mode='alert', title='Alert')
        task = Item.objects.create(mode='task', title='Task')

        self.assertIsNotNone(SilencingService.find_matching_rule(alert))
        self.assertIsNone(SilencingService.find_matching_rule(task))

    def test_find_matching_rule_by_item_type_wildcard(self):
        """Test finding silence rule by item_type with wildcard."""
        SilenceRule.objects.create(
            name='Silence Hedging Actions',
            item_types=['action.hedging.*'],
            is_active=True
        )

        action1 = Item.objects.create(
            mode='action',
            title='Short Hedge',
            item_type='action.hedging.short'
        )
        action2 = Item.objects.create(
            mode='action',
            title='Long Hedge',
            item_type='action.hedging.long'
        )
        action3 = Item.objects.create(
            mode='action',
            title='Other Action',
            item_type='action.other'
        )

        self.assertIsNotNone(SilencingService.find_matching_rule(action1))
        self.assertIsNotNone(SilencingService.find_matching_rule(action2))
        self.assertIsNone(SilencingService.find_matching_rule(action3))

    def test_find_matching_rule_by_severity(self):
        """Test finding silence rule by severity."""
        SilenceRule.objects.create(
            name='Silence Info Alerts',
            severities=['info'],
            is_active=True
        )

        info_alert = Item.objects.create(
            mode='alert',
            title='Info Alert',
            severity='info'
        )
        warning_alert = Item.objects.create(
            mode='alert',
            title='Warning Alert',
            severity='warning'
        )

        self.assertIsNotNone(SilencingService.find_matching_rule(info_alert))
        self.assertIsNone(SilencingService.find_matching_rule(warning_alert))

    def test_find_matching_rule_by_labels(self):
        """Test finding silence rule by label matchers."""
        SilenceRule.objects.create(
            name='Silence Production',
            label_matchers={'env': 'production'},
            is_active=True
        )

        prod_item = Item.objects.create(
            title='Prod Item',
            labels={'env': 'production', 'host': 'web-01'}
        )
        dev_item = Item.objects.create(
            title='Dev Item',
            labels={'env': 'development'}
        )

        self.assertIsNotNone(SilencingService.find_matching_rule(prod_item))
        self.assertIsNone(SilencingService.find_matching_rule(dev_item))

    def test_find_matching_rule_with_wildcard_labels(self):
        """Test finding silence rule with wildcard label values."""
        SilenceRule.objects.create(
            name='Silence Web Hosts',
            label_matchers={'host': 'web-*'},
            is_active=True
        )

        web_item = Item.objects.create(
            title='Web Alert',
            labels={'host': 'web-01'}
        )
        db_item = Item.objects.create(
            title='DB Alert',
            labels={'host': 'db-01'}
        )

        self.assertIsNotNone(SilencingService.find_matching_rule(web_item))
        self.assertIsNone(SilencingService.find_matching_rule(db_item))

    def test_find_matching_rule_time_bounds(self):
        """Test silence rule respects time bounds."""
        # Rule that hasn't started yet
        future_rule = SilenceRule.objects.create(
            name='Future Silence',
            starts_at=timezone.now() + timedelta(hours=1),
            is_active=True
        )

        # Rule that has ended
        past_rule = SilenceRule.objects.create(
            name='Past Silence',
            ends_at=timezone.now() - timedelta(hours=1),
            is_active=True
        )

        # Active rule
        active_rule = SilenceRule.objects.create(
            name='Active Silence',
            starts_at=timezone.now() - timedelta(hours=1),
            ends_at=timezone.now() + timedelta(hours=1),
            sources=['test'],
            is_active=True
        )

        item = Item.objects.create(title='Test Item', source='test')

        rule = SilencingService.find_matching_rule(item)
        self.assertIsNotNone(rule)
        self.assertEqual(rule.name, 'Active Silence')

    def test_find_matching_rule_inactive_rule(self):
        """Test inactive silence rule is not matched."""
        SilenceRule.objects.create(
            name='Inactive Rule',
            sources=['test'],
            is_active=False
        )

        item = Item.objects.create(title='Test', source='test')

        self.assertIsNone(SilencingService.find_matching_rule(item))

    def test_silence_item(self):
        """Test silencing an item directly."""
        item = Item.objects.create(title='To Silence')

        result = SilencingService.silence_item(
            item,
            duration_minutes=60,
            silenced_by='user:admin',
            reason='Maintenance'
        )

        self.assertTrue(result.is_silenced())
        self.assertEqual(result.silenced_by, 'user:admin')
        self.assertEqual(result.silence_reason, 'Maintenance')

    def test_silence_item_with_until(self):
        """Test silencing an item with specific end time."""
        item = Item.objects.create(title='To Silence')
        until = timezone.now() + timedelta(hours=2)

        result = SilencingService.silence_item(item, until=until)

        self.assertEqual(result.silenced_until, until)

    def test_silence_item_requires_duration_or_until(self):
        """Test silencing requires duration or until."""
        item = Item.objects.create(title='Test')

        with self.assertRaises(ValueError):
            SilencingService.silence_item(item)

    def test_create_silence_rule(self):
        """Test creating a silence rule."""
        rule = SilencingService.create_silence_rule(
            name='Test Rule',
            duration_minutes=120,
            modes=['alert'],
            sources=['prometheus'],
            label_matchers={'env': 'production'},
            created_by='user:admin',
            reason='Maintenance window'
        )

        self.assertIsNotNone(rule.id)
        self.assertTrue(rule.is_active)
        self.assertIsNotNone(rule.starts_at)
        self.assertIsNotNone(rule.ends_at)

    def test_match_count_increments(self):
        """Test that match_count increments on rule match."""
        rule = SilenceRule.objects.create(
            name='Counter Rule',
            sources=['test'],
            is_active=True
        )

        item = Item.objects.create(title='Test', source='test')

        SilencingService.find_matching_rule(item)
        rule.refresh_from_db()
        self.assertEqual(rule.match_count, 1)

        SilencingService.find_matching_rule(item)
        rule.refresh_from_db()
        self.assertEqual(rule.match_count, 2)

    def test_cleanup_expired_silences(self):
        """Test cleanup of expired silence rules."""
        # Create expired rule
        expired = SilenceRule.objects.create(
            name='Expired',
            ends_at=timezone.now() - timedelta(hours=1),
            is_active=True
        )

        # Create active rule
        active = SilenceRule.objects.create(
            name='Active',
            ends_at=timezone.now() + timedelta(hours=1),
            is_active=True
        )

        count = SilencingService.cleanup_expired_silences()

        self.assertEqual(count, 1)
        expired.refresh_from_db()
        active.refresh_from_db()
        self.assertFalse(expired.is_active)
        self.assertTrue(active.is_active)


# =============================================================================
# Escalation Service Tests
# =============================================================================

class EscalationServiceTests(TestCase):
    """Test EscalationService functionality."""

    def setUp(self):
        """Set up test escalation rule."""
        self.rule = EscalationRule.objects.create(
            name='Test Escalation',
            modes=['alert'],
            min_severity='warning',
            stages=[
                {
                    'delay_minutes': 5,
                    'actions': [
                        {'type': 'bump_severity', 'to': 'error'}
                    ]
                },
                {
                    'delay_minutes': 15,
                    'actions': [
                        {'type': 'bump_severity', 'to': 'critical'},
                        {'type': 'add_channels', 'channels': ['phone_call']}
                    ]
                }
            ],
            is_active=True
        )

    def test_find_matching_rule(self):
        """Test finding matching escalation rule."""
        alert = Item.objects.create(
            mode='alert',
            title='Warning Alert',
            severity='warning'
        )

        rule = EscalationService.find_matching_rule(alert)
        self.assertEqual(rule, self.rule)

    def test_find_matching_rule_no_match(self):
        """Test no matching rule when criteria don't match."""
        alert = Item.objects.create(
            mode='alert',
            title='Info Alert',
            severity='info'  # Below min_severity
        )

        rule = EscalationService.find_matching_rule(alert)
        self.assertIsNone(rule)

    def test_find_matching_rule_by_item_type(self):
        """Test finding rule by item_type pattern."""
        rule = EscalationRule.objects.create(
            name='Database Escalation',
            item_types=['alert.database.*'],
            is_active=True
        )

        db_alert = Item.objects.create(
            mode='alert',
            title='DB Alert',
            item_type='alert.database.connection'
        )

        matched = EscalationService.find_matching_rule(db_alert)
        self.assertEqual(matched, rule)

    def test_get_next_escalation_stage(self):
        """Test getting next escalation stage."""
        alert = Item.objects.create(
            mode='alert',
            title='Test',
            escalation_level=0
        )

        stage = EscalationService.get_next_escalation_stage(alert, self.rule)

        self.assertIsNotNone(stage)
        self.assertEqual(stage['delay_minutes'], 5)

    def test_get_next_escalation_stage_level_1(self):
        """Test getting second escalation stage."""
        alert = Item.objects.create(
            mode='alert',
            title='Test',
            escalation_level=1
        )

        stage = EscalationService.get_next_escalation_stage(alert, self.rule)

        self.assertIsNotNone(stage)
        self.assertEqual(stage['delay_minutes'], 15)

    def test_get_next_escalation_stage_no_more(self):
        """Test no more stages when fully escalated."""
        alert = Item.objects.create(
            mode='alert',
            title='Test',
            escalation_level=2  # Past last stage
        )

        stage = EscalationService.get_next_escalation_stage(alert, self.rule)
        self.assertIsNone(stage)

    def test_should_escalate_not_enough_time(self):
        """Test should_escalate returns False when not enough time passed."""
        alert = Item.objects.create(
            mode='alert',
            title='Test',
            first_fired_at=timezone.now()  # Just fired
        )

        should = EscalationService.should_escalate(alert, self.rule)
        self.assertFalse(should)

    def test_should_escalate_enough_time_passed(self):
        """Test should_escalate returns True after delay."""
        alert = Item.objects.create(
            mode='alert',
            title='Test',
            first_fired_at=timezone.now() - timedelta(minutes=10)  # 10 min ago
        )

        should = EscalationService.should_escalate(alert, self.rule)
        self.assertTrue(should)

    def test_escalate_item_bumps_severity(self):
        """Test escalate_item bumps severity."""
        alert = Item.objects.create(
            mode='alert',
            title='Test',
            severity='warning',
            first_fired_at=timezone.now() - timedelta(minutes=10)
        )

        result = EscalationService.escalate_item(alert, self.rule)

        self.assertTrue(result)
        alert.refresh_from_db()
        self.assertEqual(alert.severity, 'error')
        self.assertEqual(alert.escalation_level, 1)
        self.assertIsNotNone(alert.last_escalated_at)

    def test_escalate_item_adds_channels(self):
        """Test escalate_item adds notification channels."""
        alert = Item.objects.create(
            mode='alert',
            title='Test',
            severity='error',
            escalation_level=1,  # Already at level 1
            last_escalated_at=timezone.now() - timedelta(minutes=20)
        )

        result = EscalationService.escalate_item(alert, self.rule)

        self.assertTrue(result)
        alert.refresh_from_db()
        self.assertEqual(alert.severity, 'critical')
        self.assertEqual(alert.escalation_level, 2)
        self.assertIn(
            'phone_call',
            alert.notification_preferences.get('escalation_channels', [])
        )

    def test_escalate_item_no_more_stages(self):
        """Test escalate_item returns False when no more stages."""
        alert = Item.objects.create(
            mode='alert',
            title='Test',
            escalation_level=5  # Way past stages
        )

        result = EscalationService.escalate_item(alert, self.rule)
        self.assertFalse(result)

    def test_escalate_item_sets_rule_reference(self):
        """Test escalate_item sets escalation_rule on item."""
        alert = Item.objects.create(
            mode='alert',
            title='Test',
            severity='warning',
            first_fired_at=timezone.now() - timedelta(minutes=10)
        )

        EscalationService.escalate_item(alert, self.rule)

        alert.refresh_from_db()
        self.assertEqual(alert.escalation_rule, self.rule)

    def test_escalate_item_set_priority(self):
        """Test escalate_item can set priority."""
        rule = EscalationRule.objects.create(
            name='Priority Escalation',
            stages=[
                {
                    'delay_minutes': 0,
                    'actions': [{'type': 'set_priority', 'to': 'urgent'}]
                }
            ],
            is_active=True
        )

        item = Item.objects.create(
            title='Test',
            priority='normal',
            created_at=timezone.now() - timedelta(minutes=5)
        )

        EscalationService.escalate_item(item, rule)

        item.refresh_from_db()
        self.assertEqual(item.priority, 'urgent')

    def test_schedule_escalation(self):
        """Test schedule_escalation returns timing info."""
        alert = Item.objects.create(
            mode='alert',
            title='Test',
            severity='warning'
        )

        schedule = EscalationService.schedule_escalation(alert)

        self.assertIsNotNone(schedule)
        self.assertEqual(schedule['delay_minutes'], 5)
        self.assertEqual(schedule['stage'], 1)
        self.assertEqual(schedule['rule_name'], 'Test Escalation')

    def test_schedule_escalation_no_rule(self):
        """Test schedule_escalation returns None when no matching rule."""
        item = Item.objects.create(
            mode='task',  # No escalation rule for tasks
            title='Test'
        )

        schedule = EscalationService.schedule_escalation(item)
        self.assertIsNone(schedule)


# =============================================================================
# Inhibition Service Tests
# =============================================================================

class InhibitionServiceTests(TestCase):
    """Test InhibitionService functionality."""

    def setUp(self):
        """Set up test inhibition rule."""
        self.rule = InhibitionRule.objects.create(
            name='Database Cascade Inhibition',
            source_item_types=['alert.database.*'],
            source_severities=['critical'],
            target_item_types=['alert.api.*'],
            equal_labels=['env', 'cluster'],
            is_active=True
        )

    def test_is_inhibited_with_firing_source(self):
        """Test alert is inhibited when source is firing."""
        # Create firing source alert
        source_alert = Item.objects.create(
            mode='alert',
            title='Database Down',
            item_type='alert.database.connection',
            severity='critical',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        # Create target alert
        target_alert = Item.objects.create(
            mode='alert',
            title='API Timeout',
            item_type='alert.api.timeout',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        is_inhibited = InhibitionService.is_inhibited(target_alert)
        self.assertTrue(is_inhibited)

    def test_is_inhibited_no_source_firing(self):
        """Test alert is not inhibited when no source is firing."""
        # Create target alert only (no source)
        target_alert = Item.objects.create(
            mode='alert',
            title='API Timeout',
            item_type='alert.api.timeout',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        is_inhibited = InhibitionService.is_inhibited(target_alert)
        self.assertFalse(is_inhibited)

    def test_is_inhibited_different_labels(self):
        """Test alert is not inhibited when labels don't match."""
        # Create firing source alert
        Item.objects.create(
            mode='alert',
            title='Database Down',
            item_type='alert.database.connection',
            severity='critical',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        # Create target alert with different cluster
        target_alert = Item.objects.create(
            mode='alert',
            title='API Timeout',
            item_type='alert.api.timeout',
            status='firing',
            labels={'env': 'production', 'cluster': 'secondary'}  # Different cluster
        )

        is_inhibited = InhibitionService.is_inhibited(target_alert)
        self.assertFalse(is_inhibited)

    def test_is_inhibited_source_resolved(self):
        """Test alert is not inhibited when source is resolved."""
        # Create resolved source alert
        Item.objects.create(
            mode='alert',
            title='Database Down',
            item_type='alert.database.connection',
            severity='critical',
            status='resolved',  # Resolved
            labels={'env': 'production', 'cluster': 'main'}
        )

        target_alert = Item.objects.create(
            mode='alert',
            title='API Timeout',
            item_type='alert.api.timeout',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        is_inhibited = InhibitionService.is_inhibited(target_alert)
        self.assertFalse(is_inhibited)

    def test_is_inhibited_non_alert_mode(self):
        """Test is_inhibited returns False for non-alert items."""
        task = Item.objects.create(mode='task', title='Task')
        self.assertFalse(InhibitionService.is_inhibited(task))

    def test_is_inhibited_source_acknowledged(self):
        """Test alert is inhibited when source is acknowledged."""
        # Acknowledged alerts should still inhibit
        Item.objects.create(
            mode='alert',
            title='Database Down',
            item_type='alert.database.connection',
            severity='critical',
            status='acknowledged',  # Acknowledged, not firing
            labels={'env': 'production', 'cluster': 'main'}
        )

        target_alert = Item.objects.create(
            mode='alert',
            title='API Timeout',
            item_type='alert.api.timeout',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        is_inhibited = InhibitionService.is_inhibited(target_alert)
        self.assertTrue(is_inhibited)

    def test_get_inhibiting_alerts(self):
        """Test getting list of alerts inhibiting a target."""
        source1 = Item.objects.create(
            mode='alert',
            title='Database Down 1',
            item_type='alert.database.connection',
            severity='critical',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        target_alert = Item.objects.create(
            mode='alert',
            title='API Timeout',
            item_type='alert.api.timeout',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        inhibitors = InhibitionService.get_inhibiting_alerts(target_alert)

        self.assertEqual(len(inhibitors), 1)
        self.assertEqual(inhibitors[0].id, source1.id)

    def test_get_inhibited_alerts(self):
        """Test getting list of alerts inhibited by a source."""
        source_alert = Item.objects.create(
            mode='alert',
            title='Database Down',
            item_type='alert.database.connection',
            severity='critical',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        target1 = Item.objects.create(
            mode='alert',
            title='API Timeout 1',
            item_type='alert.api.timeout',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )
        target2 = Item.objects.create(
            mode='alert',
            title='API Timeout 2',
            item_type='alert.api.error',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        inhibited = InhibitionService.get_inhibited_alerts(source_alert)

        self.assertEqual(len(inhibited), 2)

    def test_inhibition_with_label_matchers(self):
        """Test inhibition with source/target label matchers."""
        rule = InhibitionRule.objects.create(
            name='Label Matcher Rule',
            source_label_matchers={'type': 'infrastructure'},
            target_label_matchers={'downstream': 'true'},
            is_active=True
        )

        source = Item.objects.create(
            mode='alert',
            title='Infra Alert',
            status='firing',
            labels={'type': 'infrastructure'}
        )

        target_match = Item.objects.create(
            mode='alert',
            title='Downstream Alert',
            status='firing',
            labels={'downstream': 'true'}
        )

        target_no_match = Item.objects.create(
            mode='alert',
            title='Other Alert',
            status='firing',
            labels={'downstream': 'false'}
        )

        self.assertTrue(InhibitionService.is_inhibited(target_match))
        self.assertFalse(InhibitionService.is_inhibited(target_no_match))

    def test_inactive_rule_not_evaluated(self):
        """Test inactive inhibition rule is not evaluated."""
        self.rule.is_active = False
        self.rule.save()

        Item.objects.create(
            mode='alert',
            title='Database Down',
            item_type='alert.database.connection',
            severity='critical',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        target_alert = Item.objects.create(
            mode='alert',
            title='API Timeout',
            item_type='alert.api.timeout',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        is_inhibited = InhibitionService.is_inhibited(target_alert)
        self.assertFalse(is_inhibited)

    def test_wildcard_item_type_matching(self):
        """Test wildcard matching for item types."""
        Item.objects.create(
            mode='alert',
            title='Database Error',
            item_type='alert.database.query_timeout',  # Should match alert.database.*
            severity='critical',
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        target = Item.objects.create(
            mode='alert',
            title='API Error',
            item_type='alert.api.response_error',  # Should match alert.api.*
            status='firing',
            labels={'env': 'production', 'cluster': 'main'}
        )

        self.assertTrue(InhibitionService.is_inhibited(target))
