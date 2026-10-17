from datetime import timedelta
from io import StringIO
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from tasks.models import (
    ExternalResponseHoldAudit,
    ExternalResponseHoldAuditArchive,
    ExternalResponseHoldWake,
    ExternalWaitAuthority,
    Item,
    ItemLink,
    Project,
    ProjectMembership,
    TeamRoster,
)
from tasks.services.actionable_items import filter_active_queue_items
from tasks.services.external_wait import (
    admit_external_response_hold,
    enqueue_external_response_wake,
    process_external_response_wake,
)
from tasks.serializers import ItemSerializer
from tasks.tasks import (
    archive_external_response_hold_audit,
    sweep_external_response_holds,
)
from tasks.views import ItemViewSet


User = get_user_model()


@override_settings(
    PULSE_EXTERNAL_RESPONSE_HOLD_ENABLED=True,
    PULSE_EXTERNAL_RESPONSE_ACTOR_ACTIVE_LIMIT=20,
    PULSE_EXTERNAL_RESPONSE_ORG_ACTIVE_LIMIT=500,
    PULSE_EXTERNAL_RESPONSE_ACTOR_RATE_PER_MINUTE=10,
)
class PLS962ExternalResponseHoldTests(TestCase):
    """Production-order branch-3 regression across two top-level attempts."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Response org', key='RSP', org_id=1,
            access_level='private', is_active=True,
        )
        self.owner = User.objects.create_user(
            username='hold-owner', email='owner@example.com', password='x',
        )
        self.owner.organization_memberships = {1: 'member'}
        ProjectMembership.objects.create(
            project=self.project, user_id=self.owner.id,
            user_email=self.owner.email, role='member', can_browse=True,
            can_create=True, can_edit=True, can_delete=False,
        )
        roster, _ = TeamRoster.objects.get_or_create(
            organization_id=self.project.org_id,
            team_name='engineering',
            defaults={'tiers': [[self.owner.email]], 'is_active': True},
        )
        roster.tiers = [[self.owner.email]]
        roster.is_active = True
        roster.save(update_fields=['tiers', 'is_active', 'updated_at'])
        self.outsider = User.objects.create_user(
            username='hold-outsider', email='outsider@example.com', password='x',
        )
        self.outsider.organization_memberships = {1: 'member'}
        ProjectMembership.objects.create(
            project=self.project, user_id=self.outsider.id,
            user_email=self.outsider.email, role='member', can_browse=True,
            can_create=True, can_edit=True, can_delete=False,
        )
        self.item = Item.objects.create(
            project=self.project, item_key='RSP-1', mode='task',
            title='Reviewed response window', status='in_progress',
            workflow_status='In Progress', assignment_group='engineering',
        )
        Item.objects.filter(pk=self.item.pk).update(
            assignee_id=self.owner.id, assignee_email=self.owner.email,
        )
        self.item.refresh_from_db()
        self.evidence = Item.objects.create(
            project=self.project, item_key='RSP-2', mode='task',
            title='Immutable batch status', status='in_progress',
        )
        self.dependent = Item.objects.create(
            project=self.project, item_key='RSP-3', mode='task',
            title='Must remain blocked', status='open',
        )
        ItemLink.objects.create(
            source_item=self.item, target_item=self.dependent, link_type='blocks',
            created_by_id=self.owner.id, created_by_email=self.owner.email,
        )
        Item.objects.filter(pk=self.dependent.pk).update(status='blocked')
        self.dependent.refresh_from_db()
        self.auth = patch('tasks.services.external_wait._authorize_item')
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.lane = patch(
            'tasks.services.external_wait.WorkflowService._is_team_member',
            side_effect=lambda email, group, item=None: email == self.owner.email,
        )
        self.lane.start()
        self.addCleanup(self.lane.stop)
        self.restore = patch(
            'tasks.services.external_wait._safe_restore_target',
            side_effect=lambda item, authority: (
                authority.pre_status, None, '', authority.pre_assignment_group
            ),
        )
        self.restore.start()
        self.addCleanup(self.restore.stop)
        self.factory = APIRequestFactory()

    def _admit(self, key='admit:RSP-1:window-1'):
        return admit_external_response_hold(
            self.item,
            user=self.owner,
            checkpoint_at=timezone.now() + timedelta(days=5),
            source_evidence={
                'kind': 'batch_status_v1',
                'item_id': self.evidence.pk,
            },
            display_reference={
                'kind': 'batch_window',
                'reference_id': 'batch-2026-08',
                'redacted_label': 'August reviewed batch',
            },
            correlation_key=key,
        )

    def test_admit_excludes_two_selections_and_evidence_wake_resumes_once(self):
        admitted = self._admit()
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'in_progress')
        self.assertNotEqual(self.item.status, 'waiting_external')
        for _attempt in range(2):
            self.assertTrue(
                filter_active_queue_items(
                    Item.objects.filter(pk=self.item.pk)
                ).exists()
            )
        self.dependent.refresh_from_db()
        self.assertEqual(self.dependent.status, 'blocked')

        # New immutable evidence snapshot is the authorized wake boundary.
        self.evidence.priority = 'high'
        self.evidence.save(update_fields=['priority'])
        wake = enqueue_external_response_wake(
            self.item,
            generation=admitted['wait_generation'],
            kind=ExternalResponseHoldWake.KIND_EVIDENCE,
            correlation_key='wake:RSP-1:reply-1',
            user=self.owner,
            evidence={
                'kind': 'external_reply_v1',
                'item_id': self.evidence.pk,
            },
        )
        self.assertEqual(process_external_response_wake(wake['wake_id']), 'applied')
        self.assertEqual(process_external_response_wake(wake['wake_id']), 'applied')
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'in_progress')
        self.assertEqual(
            ExternalResponseHoldAudit.objects.filter(
                authority__item=self.item, event_type='resume',
            ).count(),
            1,
        )
        self.dependent.refresh_from_db()
        self.assertEqual(self.dependent.status, 'blocked')

    def test_duplicate_admission_replays_but_correlation_conflict_refuses(self):
        first = self._admit()
        replay = self._admit()
        self.assertEqual(first['receipt'], replay['receipt'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(
            ExternalWaitAuthority.objects.filter(item=self.item).count(), 1
        )

        self.evidence.priority = 'high'
        self.evidence.save(update_fields=['priority'])
        enqueue_external_response_wake(
            self.item,
            generation=first['wait_generation'],
            kind=ExternalResponseHoldWake.KIND_EVIDENCE,
            correlation_key='wake:RSP-1:conflict',
            user=self.owner,
            evidence={'kind': 'external_reply_v1', 'item_id': self.evidence.pk},
        )
        with patch(
            'tasks.services.external_wait.WorkflowService._is_team_member',
            return_value=True,
        ):
            with self.assertRaisesMessage(
                ValidationError, 'correlation_payload_conflict'
            ):
                enqueue_external_response_wake(
                    self.item,
                    generation=first['wait_generation'],
                    kind=ExternalResponseHoldWake.KIND_EVIDENCE,
                    correlation_key='wake:RSP-1:conflict',
                    user=self.outsider,
                    evidence={
                        'kind': 'external_reply_v1', 'item_id': self.evidence.pk,
                    },
                )

    def test_wrong_principal_wrong_org_mutable_and_past_inputs_refuse(self):
        with self.assertRaises(PermissionDenied):
            admit_external_response_hold(
                self.item,
                user=self.outsider,
                checkpoint_at=timezone.now() + timedelta(days=2),
                source_evidence={
                    'kind': 'batch_status_v1', 'item_id': self.evidence.pk,
                },
                display_reference={
                    'kind': 'batch_window', 'reference_id': 'batch',
                    'redacted_label': 'Reviewed batch',
                },
                correlation_key='admit:RSP-1:outsider',
            )
        foreign = Project.objects.create(
            name='Foreign', key='FOR', org_id=2,
            access_level='private', is_active=True,
        )
        foreign_evidence = Item.objects.create(
            project=foreign, item_key='FOR-1', mode='task', title='Foreign',
        )
        with self.assertRaises(PermissionDenied):
            admit_external_response_hold(
                self.item,
                user=self.owner,
                checkpoint_at=timezone.now() + timedelta(days=2),
                source_evidence={
                    'kind': 'batch_status_v1', 'item_id': foreign_evidence.pk,
                },
                display_reference={
                    'kind': 'batch_window', 'reference_id': 'batch',
                    'redacted_label': 'Reviewed batch',
                },
                correlation_key='admit:RSP-1:foreign',
            )
        for bad in (
            {'kind': 'comment_v1', 'item_id': self.evidence.pk},
            {'kind': 'batch_status_v1', 'comment': 'mutable'},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                admit_external_response_hold(
                    self.item,
                    user=self.owner,
                    checkpoint_at=timezone.now() + timedelta(days=2),
                    source_evidence=bad,
                    display_reference={
                        'kind': 'batch_window', 'reference_id': 'batch',
                        'redacted_label': 'Reviewed batch',
                    },
                    correlation_key='admit:RSP-1:bad-evidence',
                )
        with self.assertRaisesMessage(ValidationError, 'checkpoint_must_be_future'):
            admit_external_response_hold(
                self.item,
                user=self.owner,
                checkpoint_at=timezone.now() - timedelta(seconds=1),
                source_evidence={
                    'kind': 'batch_status_v1', 'item_id': self.evidence.pk,
                },
                display_reference={
                    'kind': 'batch_window', 'reference_id': 'batch',
                    'redacted_label': 'Reviewed batch',
                },
                correlation_key='admit:RSP-1:past',
            )

    def test_early_checkpoint_refuses_and_stale_old_generation_cannot_overwrite(self):
        admitted = self._admit()
        wake = enqueue_external_response_wake(
            self.item,
            generation=admitted['wait_generation'],
            kind=ExternalResponseHoldWake.KIND_CHECKPOINT,
            correlation_key='checkpoint:RSP-1:early',
            service_identity='pulse-checkpoint-scheduler',
        )
        self.assertEqual(process_external_response_wake(wake['wake_id']), 'refused')
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'in_progress')
        self.assertNotEqual(self.item.status, 'waiting_external')

        revoke = enqueue_external_response_wake(
            self.item,
            generation=admitted['wait_generation'],
            kind=ExternalResponseHoldWake.KIND_REVOKE,
            correlation_key='revoke:RSP-1:one',
            user=self.owner,
        )
        self.assertEqual(process_external_response_wake(revoke['wake_id']), 'applied')
        self.assertEqual(process_external_response_wake(wake['wake_id']), 'refused')
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'in_progress')

    def test_owner_removal_after_enqueue_refuses_at_reducer_boundary(self):
        admitted = self._admit()
        self.evidence.priority = 'high'
        self.evidence.save(update_fields=['priority'])
        wake = enqueue_external_response_wake(
            self.item,
            generation=admitted['wait_generation'],
            kind=ExternalResponseHoldWake.KIND_EVIDENCE,
            correlation_key='wake:RSP-1:owner-removed',
            user=self.owner,
            evidence={'kind': 'external_reply_v1', 'item_id': self.evidence.pk},
        )
        self.item.refresh_from_db()
        self.item.assignee_id = None
        self.item.assignee_email = None
        self.item.assignment_group = 'product'
        Item.objects.filter(pk=self.item.pk).update(
            assignee_id=None, assignee_email=None, assignment_group='product',
        )
        with patch(
            'tasks.services.external_wait.WorkflowService._is_team_member',
            return_value=False,
        ):
            self.assertEqual(
                process_external_response_wake(wake['wake_id']), 'refused'
            )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'in_progress')
        self.assertNotEqual(self.item.status, 'waiting_external')

    def test_serializer_is_redacted_and_scheduler_capability_is_closed(self):
        admitted = self._admit()
        self.item.refresh_from_db()
        current = ItemSerializer(self.item).data['external_wait']['current']
        self.assertEqual(current['branch'], 'external_response_hold_v1')
        self.assertNotIn('source_evidence_digest', current)
        self.assertNotIn('evidence', current)
        self.assertNotIn('assignee_email', current)
        self.assertEqual(
            current['display_reference']['redacted_label'],
            'August reviewed batch',
        )
        with self.assertRaises(PermissionDenied):
            enqueue_external_response_wake(
                self.item,
                generation=admitted['wait_generation'],
                kind=ExternalResponseHoldWake.KIND_CHECKPOINT,
                correlation_key='checkpoint:RSP-1:forged',
                user=self.owner,
            )

    def test_api_rejects_caller_identity_and_audit_read_is_scoped_redacted(self):
        admit_view = ItemViewSet.as_view({'post': 'admit_external_response_hold'})
        forged = self.factory.post(
            f'/api/items/{self.item.pk}/admit-external-response-hold/',
            {
                'checkpoint_at': (timezone.now() + timedelta(days=2)).isoformat(),
                'source_evidence': {
                    'kind': 'batch_status_v1', 'item_id': self.evidence.pk,
                },
                'display_reference': {
                    'kind': 'batch_window', 'reference_id': 'batch',
                    'redacted_label': 'Reviewed batch',
                },
                'correlation_key': 'admit:RSP-1:forged-identity',
                'organization_id': self.project.org_id,
            },
            format='json',
        )
        force_authenticate(forged, user=self.owner)
        response = admit_view(forged, pk=self.item.pk)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'caller_supplied_identity_forbidden')
        self.assertFalse(ExternalWaitAuthority.objects.filter(item=self.item).exists())

        self._admit()
        audit_view = ItemViewSet.as_view({'get': 'external_response_hold_audit'})
        denied = self.factory.get(
            f'/api/items/{self.item.pk}/external-response-hold-audit/'
        )
        force_authenticate(denied, user=self.outsider)
        with patch(
            'tasks.services.workflow_service.WorkflowService._is_team_member',
            return_value=False,
        ):
            denied_response = audit_view(denied, pk=self.item.pk)
        self.assertEqual(denied_response.status_code, 403)

        allowed = self.factory.get(
            f'/api/items/{self.item.pk}/external-response-hold-audit/'
        )
        force_authenticate(allowed, user=self.owner)
        with patch(
            'tasks.services.workflow_service.WorkflowService._is_team_member',
            side_effect=lambda email, group, item=None: email == self.owner.email,
        ):
            allowed_response = audit_view(allowed, pk=self.item.pk)
        self.assertEqual(allowed_response.status_code, 200)
        self.assertEqual(len(allowed_response.data), 1)
        row = allowed_response.data[0]
        self.assertEqual(
            set(row),
            {
                'event_type', 'wait_generation', 'correlation_key',
                'reason_code', 'created_at',
            },
        )

    def test_broker_enqueue_failure_keeps_durable_wake_for_sweep_replay(self):
        admitted = self._admit()
        self.evidence.priority = 'high'
        self.evidence.save(update_fields=['priority'])
        with override_settings(TESTING=False), patch(
            'tasks.tasks.deliver_external_response_hold_wake.delay',
            side_effect=RuntimeError('broker unavailable'),
        ):
            # A commit-hook broker failure must not roll back the durable wake.
            with self.assertRaisesMessage(RuntimeError, 'broker unavailable'):
                with self.captureOnCommitCallbacks(execute=True):
                    enqueue_external_response_wake(
                        self.item,
                        generation=admitted['wait_generation'],
                        kind=ExternalResponseHoldWake.KIND_EVIDENCE,
                        correlation_key='wake:RSP-1:broker-down',
                        user=self.owner,
                        evidence={
                            'kind': 'external_reply_v1',
                            'item_id': self.evidence.pk,
                        },
                    )
        wake = ExternalResponseHoldWake.objects.get(
            correlation_key='wake:RSP-1:broker-down'
        )
        self.assertEqual(wake.state, ExternalResponseHoldWake.STATE_PENDING)
        with patch(
            'tasks.tasks.deliver_external_response_hold_wake.delay'
        ) as deliver:
            replay = sweep_external_response_holds()
        self.assertEqual(replay['pending_enqueued'], 1)
        deliver.assert_called_once_with(str(wake.id))

    def test_overdue_fail_loud_is_sanitized_deduplicated_and_owner_routed(self):
        admitted = self._admit()
        authority = ExternalWaitAuthority.objects.get(item=self.item)
        wake = ExternalResponseHoldWake.objects.create(
            authority=authority,
            organization_id=self.project.org_id,
            item=self.item,
            wait_generation=admitted['wait_generation'],
            kind=ExternalResponseHoldWake.KIND_CHECKPOINT,
            correlation_key='checkpoint:RSP-1:overdue',
            service_identity='pulse-checkpoint-scheduler',
        )
        ExternalResponseHoldWake.objects.filter(pk=wake.pk).update(
            created_at=timezone.now() - timedelta(minutes=11)
        )
        alert = Item.objects.create(
            project=self.project, item_key='RSP-4', mode='alert',
            title='Pulse external-response hold wake is overdue',
            status='open',
        )
        with patch(
            'tasks.tasks.deliver_external_response_hold_wake.delay'
        ), patch(
            'tasks.services.deduplication.AlertDeduplicationService.submit_alert',
            side_effect=[(alert, True), (alert, False)],
        ) as submit, patch('tasks.tasks.send_alert_notification.delay') as notify:
            with self.captureOnCommitCallbacks(execute=True):
                first = sweep_external_response_holds()
            with self.captureOnCommitCallbacks(execute=True):
                second = sweep_external_response_holds()
        self.assertTrue(first['fail_loud'])
        self.assertTrue(second['fail_loud'])
        payload = submit.call_args_list[0].args[0]
        self.assertEqual(payload['assignee_email'], self.owner.email)
        self.assertEqual(payload['assignment_group'], 'engineering')
        self.assertNotIn('display_reference', str(payload))
        self.assertNotIn('source_evidence', str(payload))
        notify.assert_called_once_with(alert.id)

    def test_fail_loud_persistence_failure_is_logged_not_silently_green(self):
        admitted = self._admit()
        authority = ExternalWaitAuthority.objects.get(item=self.item)
        wake = ExternalResponseHoldWake.objects.create(
            authority=authority,
            organization_id=self.project.org_id,
            item=self.item,
            wait_generation=admitted['wait_generation'],
            kind=ExternalResponseHoldWake.KIND_CHECKPOINT,
            correlation_key='checkpoint:RSP-1:persist-failure',
            service_identity='pulse-checkpoint-scheduler',
        )
        ExternalResponseHoldWake.objects.filter(pk=wake.pk).update(
            created_at=timezone.now() - timedelta(minutes=11)
        )
        with patch(
            'tasks.tasks.deliver_external_response_hold_wake.delay'
        ), patch(
            'tasks.services.deduplication.AlertDeduplicationService.submit_alert',
            side_effect=RuntimeError('alert store unavailable'),
        ), self.assertLogs('tasks.tasks', level='ERROR') as logs:
            result = sweep_external_response_holds()
        self.assertFalse(result['fail_loud'])
        self.assertTrue(result['fail_loud_error'])
        self.assertTrue(any(
            'external-response fail-loud alert persistence failed' in line
            for line in logs.output
        ))

    def test_archive_is_idempotent_and_rows_are_append_only(self):
        self._admit()
        audit = ExternalResponseHoldAudit.objects.get(
            authority__item=self.item, event_type='enter'
        )
        ExternalResponseHoldAudit.objects.filter(pk=audit.pk).update(
            created_at=timezone.now() - timedelta(days=91)
        )
        first = archive_external_response_hold_audit()
        second = archive_external_response_hold_audit()
        self.assertEqual(first['archived'], 1)
        self.assertEqual(second['archived'], 0)
        archive = ExternalResponseHoldAuditArchive.objects.get(
            source_audit_id=audit.id
        )
        archive.envelope = {'tampered': True}
        with self.assertRaisesMessage(ValidationError, 'append_only'):
            archive.save()
        with self.assertRaisesMessage(ValidationError, 'append_only'):
            archive.delete()

    def test_migration_is_exact_allowlisted_and_dry_run_first(self):
        checkpoint = timezone.now() + timedelta(days=5)
        allowlist = [{
            'org': self.project.org_id,
            'carrier_id': self.item.pk,
            'checkpoint': checkpoint.isoformat(),
            'source_evidence': {
                'kind': 'batch_status_v1',
                'item_id': self.evidence.pk,
            },
            'release_policy': 'evidence_or_checkpoint_v1',
        }]
        args = {
            'carrier_id': self.item.pk,
            'checkpoint_at': checkpoint.isoformat(),
            'source_evidence_item_id': self.evidence.pk,
            'evidence_kind': 'batch_status_v1',
            'reference_kind': 'batch_window',
            'reference_id': 'batch-2026-08',
            'redacted_label': 'August reviewed batch',
            'correlation_key': 'migration:RSP-1:window-1',
            'actor_email': self.owner.email,
        }
        with patch.dict(
            'os.environ',
            {'PULSE_EXTERNAL_RESPONSE_MIGRATION_ALLOWLIST': json.dumps(allowlist)},
        ):
            output = StringIO()
            call_command('migrate_external_response_hold', stdout=output, **args)
            self.assertIn('DRY RUN', output.getvalue())
            self.item.refresh_from_db()
            self.assertNotEqual(self.item.status, 'waiting_external')
            call_command(
                'migrate_external_response_hold', apply=True, stdout=StringIO(),
                **args,
            )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, 'in_progress')
        self.assertNotEqual(self.item.status, 'waiting_external')
