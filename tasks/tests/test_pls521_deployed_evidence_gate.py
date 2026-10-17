"""PLS-521: exact deployed-evidence gate on Security Merge→Verification.

The regression mirrors the production caller order: freeze an immutable
requirement generation, ingest (or omit) one out-of-band evidence snapshot,
then execute the top-level workflow transition.  In particular, the stale
process/Redis metric case is represented by a sample from before the current
workload generation became Ready; mere series presence must not pass.
"""
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest import mock

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from tasks.models import (
    Comment,
    DeploymentEvidenceCursor,
    DeploymentEvidenceGateOutbox,
    DeploymentEvidenceGateVerdict,
    Item,
    ItemLink,
    LinkedPullRequestMergeAttestation,
    Project,
    Status,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
    VerificationRequirementManifest,
)
from tasks.services.deployment_evidence import (
    create_evidence_snapshot,
    create_requirement_manifest,
    evaluate_and_record_gate,
    invalidate_evidence,
    record_merge_attestation,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
from tasks.tasks import (
    _attempt_merge_event_retry,
    _drain_merged_awaiting_merge_retries,
    sync_linked_pr_statuses,
)


MERGE_COMMIT = 'a' * 40
ARTIFACT = 'sha256:' + ('b' * 64)
CONFIG = 'sha256:' + ('c' * 64)
RULE = 'sha256:' + ('d' * 64)
OTHER_RULE = 'sha256:' + ('e' * 64)


class DeployedEvidenceGateTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='pls521', email='pls521@shizuha.com', password='x',
            is_staff=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.project = Project.objects.create(
            name='Pulse Gate', key='P521', org_id=1, access_level='private',
        )
        awaiting_merge, _ = Status.objects.get_or_create(
            slug='awaiting_merge', defaults={
                'name': 'Awaiting Merge', 'category': 'in_progress', 'position': 1,
            },
        )
        verification, _ = Status.objects.get_or_create(
            slug='verification', defaults={
                'name': 'Awaiting Verification', 'category': 'in_progress', 'position': 2,
            },
        )
        self.workflow, _ = Workflow.objects.get_or_create(
            slug='security-finding', defaults={
                'name': 'Security Finding', 'initial_status': awaiting_merge,
            },
        )
        WorkflowStatus.objects.get_or_create(
            workflow=self.workflow, status=awaiting_merge, defaults={'position': 1},
        )
        WorkflowStatus.objects.get_or_create(
            workflow=self.workflow, status=verification, defaults={'position': 2},
        )
        self.transition = WorkflowTransition.objects.filter(
            workflow=self.workflow,
            from_status='awaiting_merge',
            to_status='verification',
        ).first()
        if not self.transition:
            self.transition = WorkflowTransition.objects.create(
                workflow=self.workflow,
                name='Merge',
                from_status='awaiting_merge',
                to_status='verification',
            )
        # Release A is deliberately expand-only: it ships the evaluator and
        # dispatcher without mutating the live transition.  Most tests below
        # exercise the eventual Release B configuration explicitly.
        validators = list(self.transition.validators or [])
        if not any(
            isinstance(validator, dict)
            and validator.get('type') == 'deployment_evidence_gate'
            for validator in validators
        ):
            validators.append({'type': 'deployment_evidence_gate'})
            WorkflowTransition.objects.filter(pk=self.transition.pk).update(
                validators=validators,
            )
            self.transition.refresh_from_db()
        self.assertEqual(
            [
                validator.get('type')
                for validator in self.transition.validators
                if isinstance(validator, dict)
            ].count('deployment_evidence_gate'),
            1,
        )

    def _item(self, *, merged=True):
        metadata = {'pull_requests': [{
            'url': 'https://origin.shizuha.com/shizuha-labs/x/pulls/1',
            'status': 'merged' if merged else 'open',
            'head_sha': 'f' * 40,
            'merge_commit_sha': MERGE_COMMIT if merged else '',
            'merged_at': timezone.now().isoformat() if merged else None,
        }]}
        item = Item.objects.create(
            project=self.project,
            item_key=f'P521-{Item.objects.filter(project=self.project).count() + 1}',
            title='Security finding',
            description='Exact deployed evidence required.',
            mode='task',
            item_type='task.security',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            workflow_name='security-finding',
            assignment_group='merge',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
            metadata=metadata,
        )
        if merged:
            record_merge_attestation(
                item=item,
                provider='forgejo',
                repo='shizuha-labs/x',
                number=1,
                url=metadata['pull_requests'][0]['url'],
                head_sha='f' * 40,
                merge_commit=MERGE_COMMIT,
                merged_at=metadata['pull_requests'][0]['merged_at'],
            )
        return item

    def _source_manifest(self, item):
        return create_requirement_manifest(
            item=item,
            merge_commit=MERGE_COMMIT,
            pull_request_repo='shizuha-labs/x',
            pull_request_number=1,
            affected_components=['source:tasks/foo.py'],
            reviewer_id=self.user.id,
            reviewer_email='security@shizuha.com',
        )

    def _live_manifest(self, item):
        return create_requirement_manifest(
            item=item,
            merge_commit=MERGE_COMMIT,
            pull_request_repo='shizuha-labs/x',
            pull_request_number=1,
            affected_components=['service:pulse'],
            scrape_targets=['pulse-api'],
            metric_series=['pulse_transition_total'],
            prometheus_rules={'PulseTransitionStalled': RULE},
            environment='live',
            service='pulse',
            workload='deploy/pulse',
            cluster='shizuha-k3s',
            namespace='pulse',
            reviewer_id=self.user.id,
            reviewer_email='security@shizuha.com',
        )

    def _evidence(self, item, *, ready_at=None, sample_at=None, rule_digest=RULE,
                  cluster='shizuha-k3s', namespace='pulse'):
        ready_at = ready_at or (timezone.now() - timedelta(minutes=1))
        sample_at = sample_at or (ready_at + timedelta(seconds=30))
        return create_evidence_snapshot(
            item=item,
            merge_commit=MERGE_COMMIT,
            artifact_digest=ARTIFACT,
            config_digest=CONFIG,
            build_provenance={
                'merge_commit': MERGE_COMMIT,
                'artifact_digest': ARTIFACT,
                'pipeline': 'origin-forgejo',
            },
            environment='live',
            service='pulse',
            cluster=cluster,
            namespace=namespace,
            workload='deploy/pulse',
            workload_uid='pulse-uid-v2',
            rollout_revision='42',
            workload_generation=7,
            observed_generation=7,
            ready=True,
            ready_at=ready_at,
            target_health={
                'pulse-api': {'up': True, 'workload_uid': 'pulse-uid-v2'},
            },
            metric_series={
                'pulse_transition_total': {
                    'sample_at': sample_at.isoformat(),
                    'workload_uid': 'pulse-uid-v2',
                },
            },
            prometheus_rules={
                'PulseTransitionStalled': {
                    'applied_semantic_digest': rule_digest,
                    'applied_generation': 3,
                },
            },
            producer_id=self.user.id,
            producer_identity='deploy-controller@shizuha.com',
        )

    def test_missing_manifest_holds_in_deploy_lane_and_persists_outbox(self):
        item = self._item()

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(
                item, 'verification', user=self.user,
                transition_id=self.transition.id,
            )

        self.assertEqual(ctx.exception.code, 'requirement_manifest_invalid')
        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')
        verdict = DeploymentEvidenceGateVerdict.objects.get(item=item)
        self.assertEqual((verdict.outcome, verdict.reason), ('hold', 'requirement_manifest_invalid'))
        self.assertTrue(DeploymentEvidenceGateOutbox.objects.filter(verdict=verdict).exists())

    def test_release_a_mixed_version_rollout_does_not_activate_gate(self):
        """Expand must converge before Release B mutates shared workflow state.

        During a rolling Release A deployment, old and new pods serve together.
        The old dispatcher ignores the new validator type, so Release A must
        leave the shared transition unchanged.  Only after every pod runs the
        new dispatcher may Release B append the validator; the second half of
        this regression models that post-convergence activation and proves the
        new dispatcher then fails closed.
        """
        WorkflowTransition.objects.filter(pk=self.transition.pk).update(validators=[])
        release_a_apps = MigrationExecutor(connection).loader.project_state([
            ('tasks', '0156_raise_active_wip_cap'),
        ]).apps
        ReleaseATransition = release_a_apps.get_model('tasks', 'WorkflowTransition')
        release_a_transition = ReleaseATransition.objects.get(pk=self.transition.pk)
        release_a_transition.name = 'Merge after Release A'
        release_a_transition.save(update_fields=['name'])
        release_a_transition.refresh_from_db()
        self.assertEqual(release_a_transition.validators, [])
        self.assertIsNone(
            WorkflowService._deployment_evidence_transition(
                self._item(), 'verification', transition_id=self.transition.id,
            )
        )

        # Only the separate Release-B migration mutates shared workflow state.
        migration = import_module(
            'tasks.migrations.0158_pls682_activate_deployment_evidence_gate'
        )
        migration.activate_deployment_evidence_gate(
            apps, SimpleNamespace(connection=connection),
        )
        self.transition.refresh_from_db()
        held = self._item()
        self._live_manifest(held)
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.execute_transition(
                held, 'verification', user=self.user,
                transition_id=self.transition.id,
            )
        held.refresh_from_db()
        self.assertEqual(held.status, 'awaiting_merge')

        passed = self._item()
        self._live_manifest(passed)
        self._evidence(passed)
        WorkflowService.execute_transition(
            passed, 'verification', user=self.user,
            transition_id=self.transition.id,
        )
        passed.refresh_from_db()
        self.assertEqual(passed.status, 'verification')

    def test_release_b_migrated_transition_holds_then_passes_exact_current_evidence(self):
        WorkflowTransition.objects.filter(pk=self.transition.pk).update(validators=[])
        migration = import_module(
            'tasks.migrations.0158_pls682_activate_deployment_evidence_gate'
        )
        migration.activate_deployment_evidence_gate(
            apps, SimpleNamespace(connection=connection),
        )
        self.transition.refresh_from_db()

        held = self._item()
        self._live_manifest(held)
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(
                held, 'verification', user=self.user,
                transition_id=self.transition.id,
            )
        self.assertEqual(ctx.exception.code, 'evidence_unreachable')
        held.refresh_from_db()
        self.assertEqual(held.status, 'awaiting_merge')

        passed = self._item()
        self._live_manifest(passed)
        self._evidence(passed)
        WorkflowService.execute_transition(
            passed, 'verification', user=self.user,
            transition_id=self.transition.id,
        )
        passed.refresh_from_db()
        self.assertEqual(passed.status, 'verification')

        source_without_attestation = self._item(merged=False)
        with self.assertRaisesRegex(ValidationError, 'provider-attested'):
            self._source_manifest(source_without_attestation)

        source = self._item()
        self._source_manifest(source)
        WorkflowService.execute_transition(
            source, 'verification', user=self.user,
            transition_id=self.transition.id,
        )
        source.refresh_from_db()
        self.assertEqual(source.status, 'verification')

    def test_source_only_manifest_passes_with_exact_merged_provenance(self):
        item = self._item()
        manifest = self._source_manifest(item)
        self.assertEqual(manifest.classification, 'source_only')

        WorkflowService.execute_transition(
            item, 'verification', user=self.user,
            transition_id=self.transition.id,
        )

        item.refresh_from_db()
        self.assertEqual(item.status, 'verification')
        self.assertEqual(
            DeploymentEvidenceGateVerdict.objects.get(item=item).outcome,
            'pass',
        )

    def test_live_acceptance_source_only_manifest_selects_source_terminal_path(self):
        """PLAT-6128: a frozen source manifest overrides inherited live proof."""
        item = self._item()
        item.acceptance_mode = 'live'
        item.save(update_fields=['acceptance_mode', 'updated_at'])
        self._source_manifest(item)

        WorkflowService._validate_deploy_verify_gate(item, 'done')

        item.refresh_from_db()
        liveness = item.metadata['mechanism_liveness']['deploy_verify_gate']
        self.assertEqual(liveness['acceptance_mode'], 'live')
        self.assertEqual(liveness['last_result'], 'pass_source_only')
        self.assertIn('evidence_path=source_only', liveness['detail'])

    def test_live_acceptance_live_manifest_still_requires_terminal_live_evidence(self):
        """PLAT-6128 negative control: genuine live requirements stay closed."""
        item = self._item()
        item.acceptance_mode = 'live'
        item.save(update_fields=['acceptance_mode', 'updated_at'])
        self._live_manifest(item)

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')

        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))
        item.refresh_from_db()
        liveness = item.metadata['mechanism_liveness']['deploy_verify_gate']
        self.assertEqual(liveness['acceptance_mode'], 'live')
        self.assertEqual(liveness['last_result'], 'block')

    def test_source_only_manifest_for_another_merge_cannot_close_current_head(self):
        """PLAT-6128 fail-closed control: manifest provenance must be current."""
        item = self._item()
        item.acceptance_mode = 'live'
        item.save(update_fields=['acceptance_mode', 'updated_at'])
        self._source_manifest(item)
        metadata = dict(item.metadata)
        metadata['pull_requests'] = [{
            **metadata['pull_requests'][0],
            'head_sha': 'e' * 40,
            'merge_commit_sha': 'd' * 40,
        }]
        item.metadata = metadata
        item.save(update_fields=['metadata', 'updated_at'])

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_deploy_verify_gate(item, 'done')

        self.assertIn('missing_correlated_deploy_event', str(ctx.exception))

    def test_untyped_component_cannot_default_to_source_only(self):
        item = self._item()
        with self.assertRaises(ValidationError):
            create_requirement_manifest(
                item=item,
                merge_commit=MERGE_COMMIT,
                pull_request_repo='shizuha-labs/x',
                pull_request_number=1,
                affected_components=['pulse'],
                reviewer_id=self.user.id,
                reviewer_email='security@shizuha.com',
            )

    def test_live_required_exact_current_snapshot_passes(self):
        item = self._item()
        self._live_manifest(item)
        ready_at = timezone.now() - timedelta(minutes=1)
        sample_at = ready_at + timedelta(seconds=30)
        evidence, created = self._evidence(
            item, ready_at=ready_at, sample_at=sample_at,
        )
        self.assertTrue(created)

        duplicate, created = self._evidence(
            item, ready_at=ready_at, sample_at=sample_at,
        )
        self.assertFalse(created)
        self.assertEqual(duplicate.pk, evidence.pk)

        WorkflowService.execute_transition(
            item, 'verification', user=self.user,
            transition_id=self.transition.id,
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'verification')

    def test_wrong_cluster_evidence_holds(self):
        # PLS-521 P1 #3: evidence from a different cluster must HOLD even when
        # env/service/workload/namespace all match the reviewed manifest.
        item = self._item()
        self._live_manifest(item)  # reviewed cluster=shizuha-k3s, namespace=pulse
        self._evidence(item, cluster='rogue-cluster')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(
                item, 'verification', user=self.user, transition_id=self.transition.id,
            )
        self.assertEqual(ctx.exception.code, 'deployment_identity_mismatch')
        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')

    def test_wrong_namespace_evidence_holds(self):
        # PLS-521 P1 #3: evidence landed in a different namespace must HOLD.
        item = self._item()
        self._live_manifest(item)
        self._evidence(item, namespace='rogue-namespace')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(
                item, 'verification', user=self.user, transition_id=self.transition.id,
            )
        self.assertEqual(ctx.exception.code, 'deployment_identity_mismatch')
        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')

    def test_plat4133_stale_process_metric_is_not_convergence(self):
        item = self._item()
        self._live_manifest(item)
        stale_sample = timezone.now() - timedelta(hours=1)
        self._evidence(item, sample_at=stale_sample)

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(
                item, 'verification', user=self.user,
                transition_id=self.transition.id,
            )

        self.assertEqual(ctx.exception.code, 'metric_stale_generation')
        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')

    def test_plat4133_hold_then_plat4138_fresh_convergence_passes(self):
        """Production order: stale snapshot → Merge HOLD → refresh → Merge PASS."""
        item = self._item()
        self._live_manifest(item)
        stale, _ = self._evidence(
            item, sample_at=timezone.now() - timedelta(hours=1),
        )

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(
                item, 'verification', user=self.user,
                transition_id=self.transition.id,
            )
        self.assertEqual(ctx.exception.code, 'metric_stale_generation')

        fresh, created = self._evidence(item)
        self.assertTrue(created)
        self.assertGreater(fresh.evidence_generation, stale.evidence_generation)

        WorkflowService.execute_transition(
            item, 'verification', user=self.user,
            transition_id=self.transition.id,
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'verification')
        self.assertEqual(
            list(
                DeploymentEvidenceGateVerdict.objects.filter(item=item)
                .order_by('evidence_generation')
                .values_list('outcome', 'reason')
            ),
            [('hold', 'metric_stale_generation'), ('pass', '')],
        )

    def test_declared_vs_controller_applied_rule_drift_holds(self):
        item = self._item()
        self._live_manifest(item)
        self._evidence(item, rule_digest=OTHER_RULE)

        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService.execute_transition(
                item, 'verification', user=self.user,
                transition_id=self.transition.id,
            )

        self.assertEqual(ctx.exception.code, 'rule_digest_mismatch')

    def test_producer_outage_is_typed_evidence_unreachable_hold(self):
        item = self._item()
        self._live_manifest(item)

        decision = evaluate_and_record_gate(
            item=item, transition=self.transition, actor=self.user,
        )

        self.assertFalse(decision.passed)
        self.assertEqual(decision.reason, 'evidence_unreachable')

    def test_new_requirement_generation_invalidates_prior_source_only_pass(self):
        item = self._item()
        first = self._source_manifest(item)
        first_decision = evaluate_and_record_gate(
            item=item, transition=self.transition, actor=self.user,
        )
        self.assertTrue(first_decision.passed)

        second = self._live_manifest(item)
        self.assertEqual(second.requirement_generation, first.requirement_generation + 1)
        second_decision = evaluate_and_record_gate(
            item=item, transition=self.transition, actor=self.user,
        )
        self.assertEqual((second_decision.outcome, second_decision.reason), ('hold', 'evidence_unreachable'))

    def test_redeploy_invalidation_advances_cursor_and_requires_fresh_evidence(self):
        # PLS-521 P1 #4 (production order): a redeploy invalidates the current
        # evidence generation — the cursor advances, the prior snapshot becomes
        # stale (gate HOLDs), and only a fresh snapshot at the new generation
        # restores a pass.
        item = self._item()
        self._live_manifest(item)
        self._evidence(item)  # generation 1
        first = evaluate_and_record_gate(item=item, transition=self.transition, actor=self.user)
        self.assertTrue(first.passed, first.reason)

        cursor = invalidate_evidence(
            item=item, reason='redeploy', actor_email='deploy-controller@shizuha.com')
        self.assertEqual(cursor.current_evidence_generation, 2)  # cursor advanced

        stale = evaluate_and_record_gate(item=item, transition=self.transition, actor=self.user)
        self.assertEqual((stale.outcome, stale.reason), ('hold', 'evidence_unreachable'))

        self._evidence(item)  # fresh snapshot at generation 2
        fresh = evaluate_and_record_gate(item=item, transition=self.transition, actor=self.user)
        self.assertTrue(fresh.passed, fresh.reason)

    def test_newer_merge_invalidation_advances_cursor_and_requires_fresh_evidence(self):
        # PLS-521 P1 #4: same production-order guarantee for a newer-merge
        # invalidation — stale evidence cannot satisfy the fresh generation.
        item = self._item()
        self._live_manifest(item)
        self._evidence(item)
        self.assertTrue(
            evaluate_and_record_gate(item=item, transition=self.transition, actor=self.user).passed)

        cursor = invalidate_evidence(
            item=item, reason='newer_merge', actor_email='deploy-controller@shizuha.com')
        self.assertEqual(cursor.current_evidence_generation, 2)

        stale = evaluate_and_record_gate(item=item, transition=self.transition, actor=self.user)
        self.assertEqual((stale.outcome, stale.reason), ('hold', 'evidence_unreachable'))

        self._evidence(item)
        self.assertTrue(
            evaluate_and_record_gate(item=item, transition=self.transition, actor=self.user).passed)

    def test_provider_sync_newer_merge_invalidates_current_evidence_in_production_order(self):
        """PASS -> provider attests a new merge -> HOLD -> fresh generation PASS.

        This exercises the authoritative ``sync_linked_pr_statuses`` caller,
        rather than invoking the lower-level invalidation helper directly.
        """
        item = self._item()
        self._live_manifest(item)
        self._evidence(item)
        self.assertTrue(
            evaluate_and_record_gate(
                item=item, transition=self.transition, actor=self.user,
            ).passed,
        )

        metadata = dict(item.metadata)
        metadata['pull_requests'][0].update({
            'repo': 'shizuha-labs/x',
            'number': 1,
        })
        metadata['pull_requests'].append({
            'url': 'https://origin.shizuha.com/shizuha-labs/x/pulls/2',
            'repo': 'shizuha-labs/x',
            'number': 2,
            'status': 'open',
        })
        item.metadata = metadata
        item.save(update_fields=['metadata'])
        # This base regression is inherited by the authority/outbox subclasses,
        # whose setUp creates an unrelated gate fixture.  Keep the reconciler's
        # production candidate set scoped to the exact item under test.
        Item.objects.exclude(pk=item.pk).update(metadata={})

        response = mock.Mock(status_code=200, ok=True)
        response.json.return_value = {
            'merged': True,
            'state': 'closed',
            'merged_at': timezone.now().isoformat(),
            'merged_by': {'login': 'merge-seat'},
            'head': {'sha': '8' * 40},
            'merge_commit_sha': '9' * 40,
        }
        with mock.patch.dict('os.environ', {
            'FORGEJO_TOKEN': 'origin-token',
            'ORIGIN_URL': 'https://origin.shizuha.com',
            'PULSE_SUBSTRATE_CAP_MODE': 'off',
        }, clear=True), mock.patch(
            'tasks.tasks.requests.get', return_value=response,
        ):
            result = sync_linked_pr_statuses.run(limit=10)

        self.assertEqual(result['checked'], 1)
        self.assertTrue(
            LinkedPullRequestMergeAttestation.objects.filter(
                item=item, repo='shizuha-labs/x', number=2,
            ).exists(),
        )
        cursor = DeploymentEvidenceCursor.objects.get(item=item)
        self.assertEqual(cursor.current_evidence_generation, 2)
        self.assertEqual(cursor.invalidation_reason, 'newer_merge')

        stale = evaluate_and_record_gate(
            item=item, transition=self.transition, actor=self.user,
        )
        self.assertEqual((stale.outcome, stale.reason), ('hold', 'evidence_unreachable'))

        self._evidence(item)
        self.assertTrue(
            evaluate_and_record_gate(
                item=item, transition=self.transition, actor=self.user,
            ).passed,
        )

    def test_forged_metadata_pull_request_cannot_satisfy_source_only_gate(self):
        item = self._item(merged=False)
        forged = dict(item.metadata)
        forged['pull_requests'][0].update({
            'status': 'merged',
            'merged_at': timezone.now().isoformat(),
            'merge_commit_sha': MERGE_COMMIT,
        })
        response = self.client.patch(
            f'/api/items/{item.pk}/', {'metadata': forged}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)

        with self.assertRaises(ValidationError) as ctx:
            self._source_manifest(item)
        self.assertIn('provider-attested', str(ctx.exception))
        self.assertFalse(
            LinkedPullRequestMergeAttestation.objects.filter(item=item).exists()
        )

    def test_two_merged_prs_select_manifest_bound_attestation_deterministically(self):
        item = self._item()
        second_commit = '9' * 40
        record_merge_attestation(
            item=item,
            provider='forgejo',
            repo='shizuha-labs/deploy',
            number=2,
            url='https://origin.shizuha.com/shizuha-labs/deploy/pulls/2',
            head_sha='8' * 40,
            merge_commit=second_commit,
            merged_at=timezone.now(),
        )
        manifest = create_requirement_manifest(
            item=item,
            merge_commit=second_commit,
            pull_request_repo='shizuha-labs/deploy',
            pull_request_number=2,
            affected_components=['source:deploy/check.py'],
            reviewer_id=self.user.id,
            reviewer_email='security@shizuha.com',
        )
        self.assertEqual(manifest.merge_attestation.repo, 'shizuha-labs/deploy')
        self.assertEqual(manifest.merge_attestation.number, 2)
        self.assertTrue(
            evaluate_and_record_gate(
                item=item, transition=self.transition, actor=self.user,
            ).passed
        )

    def test_direct_item_status_patch_cannot_bypass_transition_gate(self):
        item = self._item()
        response = self.client.patch(
            f'/api/items/{item.pk}/', {'status': 'verification'}, format='json',
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('transition-only', str(response.data))

    def test_two_request_workflow_escape_then_status_patch_is_denied(self):
        item = self._item()
        escape = self.client.patch(
            f'/api/items/{item.pk}/', {'workflow_name': 'autonomous-bug'}, format='json',
        )
        self.assertEqual(escape.status_code, 400, escape.data)
        item.refresh_from_db()
        self.assertEqual(item.workflow_name, 'security-finding')

        status_escape = self.client.patch(
            f'/api/items/{item.pk}/', {'status': 'verification'}, format='json',
        )
        self.assertEqual(status_escape.status_code, 400, status_escape.data)
        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')

    def test_direct_change_into_security_finding_workflow_is_denied(self):
        item = self._item()
        Item.objects.filter(pk=item.pk).update(workflow_name='autonomous-bug')
        item.refresh_from_db()

        response = self.client.patch(
            f'/api/items/{item.pk}/', {'workflow_name': 'security-finding'}, format='json',
        )
        self.assertEqual(response.status_code, 400, response.data)
        item.refresh_from_db()
        self.assertEqual(item.workflow_name, 'autonomous-bug')

    def test_pass_verdict_rolls_back_if_status_write_fails(self):
        item = self._item()
        self._source_manifest(item)

        with mock.patch.object(Item, 'save', side_effect=RuntimeError('status write failed')):
            with self.assertRaisesRegex(RuntimeError, 'status write failed'):
                WorkflowService.execute_transition(
                    item, 'verification', user=self.user,
                    transition_id=self.transition.id,
                )

        self.assertFalse(DeploymentEvidenceGateVerdict.objects.filter(item=item).exists())
        self.assertFalse(
            DeploymentEvidenceGateOutbox.objects.filter(verdict__item=item).exists()
        )


class ReleaseBActivationMigrationTests(TestCase):
    def setUp(self):
        awaiting_merge, _ = Status.objects.get_or_create(
            slug='awaiting_merge', defaults={
                'name': 'Awaiting Merge',
                'category': 'in_progress',
                'position': 1,
            },
        )
        verification, _ = Status.objects.get_or_create(
            slug='verification', defaults={
                'name': 'Awaiting Verification',
                'category': 'in_progress',
                'position': 2,
            },
        )
        self.workflow, _ = Workflow.objects.get_or_create(
            slug='security-finding', defaults={
                'name': 'Security Finding',
                'initial_status': awaiting_merge,
            },
        )
        WorkflowStatus.objects.get_or_create(
            workflow=self.workflow,
            status=awaiting_merge,
            defaults={'position': 1},
        )
        WorkflowStatus.objects.get_or_create(
            workflow=self.workflow,
            status=verification,
            defaults={'position': 2},
        )
        targets = WorkflowTransition.objects.filter(
            workflow=self.workflow,
            from_status='awaiting_merge',
            to_status='verification',
        ).order_by('pk')
        self.transition = targets.first()
        if self.transition is None:
            self.transition = WorkflowTransition.objects.create(
                workflow=self.workflow,
                name='Merge',
                from_status='awaiting_merge',
                to_status='verification',
            )
            targets = WorkflowTransition.objects.filter(pk=self.transition.pk)
        targets.exclude(pk=self.transition.pk).delete()
        self.other_validator = {
            'type': 'field_required',
            'field': 'assignee_email',
        }
        WorkflowTransition.objects.filter(pk=self.transition.pk).update(
            validators=[self.other_validator],
        )
        self.migration = import_module(
            'tasks.migrations.0158_pls682_activate_deployment_evidence_gate'
        )
        self.schema_editor = SimpleNamespace(connection=connection)

    def _activate(self):
        self.migration.activate_deployment_evidence_gate(
            apps, self.schema_editor,
        )

    def test_append_without_clobbering_and_reverse_removes_only_gate(self):
        self._activate()
        self.transition.refresh_from_db()
        self.assertEqual(self.transition.validators, [
            self.other_validator,
            {'type': 'deployment_evidence_gate'},
        ])

        self.migration.remove_deployment_evidence_gate(
            apps, self.schema_editor,
        )
        self.transition.refresh_from_db()
        self.assertEqual(self.transition.validators, [self.other_validator])

    def test_missing_target_fails_loud(self):
        self.transition.delete()
        with self.assertRaisesRegex(RuntimeError, 'found 0'):
            self._activate()

    def test_ambiguous_target_fails_loud(self):
        duplicate = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Second Merge route',
            from_status='awaiting_merge',
            to_status='verification',
            validators=[self.other_validator],
        )
        WorkflowTransition.objects.filter(pk=duplicate.pk).update(
            validators=[self.other_validator],
        )
        with self.assertRaisesRegex(RuntimeError, 'found 2'):
            self._activate()

    def test_malformed_target_fails_loud(self):
        WorkflowTransition.objects.filter(pk=self.transition.pk).update(
            validators={'type': 'field_required'},
        )
        with self.assertRaisesRegex(RuntimeError, 'must be a list'):
            self._activate()

    def test_duplicate_gate_state_fails_loud(self):
        WorkflowTransition.objects.filter(pk=self.transition.pk).update(
            validators=[
                {'type': 'deployment_evidence_gate'},
                {'type': 'deployment_evidence_gate'},
            ],
        )
        with self.assertRaisesRegex(RuntimeError, 'contains 2'):
            self._activate()

    def test_model_boundary_reinstates_gate_on_partial_update(self):
        self.transition.refresh_from_db()
        self.transition.name = 'Merge (renamed)'
        self.transition.save(update_fields=['name'])
        self.transition.refresh_from_db()
        self.assertEqual(self.transition.validators, [
            self.other_validator,
            {'type': 'deployment_evidence_gate'},
        ])

    def test_model_boundary_rejects_malformed_and_duplicate_state(self):
        for validators, message in [
            ({'type': 'field_required'}, 'must be a list'),
            ([{'type': 'deployment_evidence_gate'}] * 2, 'duplicate'),
        ]:
            with self.subTest(validators=validators):
                self.transition.validators = validators
                with self.assertRaisesRegex(ValidationError, message):
                    self.transition.save()


class DeployedEvidenceAuthorityTests(DeployedEvidenceGateTests):
    """PLS-521 P1 #2: the two trust-boundary writers require EXACT team
    membership. `is_staff` must NOT bypass authority; only an explicit, audited
    superuser break-glass is allowed. These exercise the real API endpoints
    (the existing gate tests call the services directly, bypassing authority)."""

    MANIFEST_URL = '/api/verification-requirements/'
    INVALIDATE_URL = '/api/deployment-evidence/invalidate/'
    # Deterministic membership: only these (email, team) pairs are members.
    _MEMBERS = {('sec@shizuha.com', 'security'), ('devops@shizuha.com', 'devops')}

    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.staff_nonmember = User.objects.create_user(
            username='p521-staffnm', email='staff-nonmember@shizuha.com',
            password='x', is_staff=True)
        self.security_member = User.objects.create_user(
            username='p521-sec', email='sec@shizuha.com', password='x')
        self.producer_member = User.objects.create_user(
            username='p521-prod', email='devops@shizuha.com', password='x')
        self.superuser = User.objects.create_user(
            username='p521-root', email='root@shizuha.com', password='x',
            is_staff=True, is_superuser=True)
        self.gate_item = self._item()

    def _fake_is_member(self, email, team_name, item=None):
        return ((email or '').strip().lower(), team_name) in self._MEMBERS

    def _post(self, user, url, payload):
        client = APIClient()
        client.force_authenticate(user)
        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._is_team_member',
            side_effect=self._fake_is_member,
        ), mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=Item.objects.all(),
        ):
            return client.post(url, payload, format='json')

    def _manifest_payload(self):
        return {'item': self.gate_item.pk, 'merge_commit': MERGE_COMMIT,
                'pull_request_repo': 'shizuha-labs/x', 'pull_request_number': 1,
                'affected_components': ['source:tasks/foo.py']}

    def _invalidate_payload(self):
        return {'item': self.gate_item.pk, 'reason': 'redeploy'}

    # ---- Manifest freeze == exact Security-team authority ----
    def test_staff_nonmember_cannot_freeze_manifest(self):
        r = self._post(self.staff_nonmember, self.MANIFEST_URL, self._manifest_payload())
        self.assertEqual(r.status_code, 403, getattr(r, 'data', r.content))

    def test_security_member_freezes_manifest(self):
        r = self._post(self.security_member, self.MANIFEST_URL, self._manifest_payload())
        self.assertEqual(r.status_code, 201, getattr(r, 'data', r.content))

    def test_manifest_post_and_get_expose_bound_cluster_and_namespace(self):
        payload = {
            **self._manifest_payload(),
            'affected_components': ['service:pulse'],
            'environment': 'live',
            'service': 'pulse',
            'workload': 'deploy/pulse',
            'cluster': 'shizuha-k3s',
            'namespace': 'pulse',
        }
        created = self._post(self.security_member, self.MANIFEST_URL, payload)
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data['cluster'], 'shizuha-k3s')
        self.assertEqual(created.data['namespace'], 'pulse')

        client = APIClient()
        client.force_authenticate(self.security_member)
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=Item.objects.all(),
        ):
            fetched = client.get(f"{self.MANIFEST_URL}{created.data['id']}/")
        self.assertEqual(fetched.status_code, 200, fetched.data)
        self.assertEqual(fetched.data['cluster'], 'shizuha-k3s')
        self.assertEqual(fetched.data['namespace'], 'pulse')

    def test_superuser_breakglass_freezes_manifest(self):
        r = self._post(self.superuser, self.MANIFEST_URL, self._manifest_payload())
        self.assertEqual(r.status_code, 201, getattr(r, 'data', r.content))

    def test_freeze_clears_pending_marker_and_triggers_retry(self):
        """PLAT-7290: freezing a manifest clears the
        merge_event_requirement_manifest_pending marker and re-queues the
        merge-event retry, so the item advances without a manual marker clear
        or re-firing PLS-521 alerts."""
        item = self._item()
        meta = dict(item.metadata or {})
        meta['merge_event_requirement_manifest_pending'] = True
        Item.objects.filter(pk=item.pk).update(metadata=meta)
        item.refresh_from_db()
        self.assertTrue(item.metadata.get('merge_event_requirement_manifest_pending'))
        payload = self._manifest_payload()
        payload['item'] = item.pk
        with mock.patch(
            'tasks.tasks._attempt_merge_event_retry'
        ) as retry:
            r = self._post(self.security_member, self.MANIFEST_URL, payload)
        self.assertEqual(r.status_code, 201, getattr(r, 'data', r.content))
        item.refresh_from_db()
        self.assertFalse(item.metadata.get('merge_event_requirement_manifest_pending'))
        retry.assert_called_once()

    def test_freeze_without_marker_does_not_trigger_retry(self):
        """PLAT-7290: freezing a manifest for an item with no pending marker is
        a no-op for the retry path (no spurious merge-event retry)."""
        item = self._item()
        self.assertFalse(item.metadata.get('merge_event_requirement_manifest_pending'))
        payload = self._manifest_payload()
        payload['item'] = item.pk
        with mock.patch(
            'tasks.tasks._attempt_merge_event_retry'
        ) as retry:
            r = self._post(self.security_member, self.MANIFEST_URL, payload)
        self.assertEqual(r.status_code, 201, getattr(r, 'data', r.content))
        retry.assert_not_called()

    # ---- Evidence invalidation == exact producer-team authority ----
    def test_staff_nonmember_cannot_invalidate_evidence(self):
        self._source_manifest(self.gate_item)  # establishes the cursor
        r = self._post(self.staff_nonmember, self.INVALIDATE_URL, self._invalidate_payload())
        self.assertEqual(r.status_code, 403, getattr(r, 'data', r.content))

    def test_producer_member_invalidates_evidence(self):
        self._source_manifest(self.gate_item)
        r = self._post(self.producer_member, self.INVALIDATE_URL, self._invalidate_payload())
        self.assertEqual(r.status_code, 200, getattr(r, 'data', r.content))

    def test_superuser_breakglass_invalidates_evidence(self):
        self._source_manifest(self.gate_item)
        r = self._post(self.superuser, self.INVALIDATE_URL, self._invalidate_payload())
        self.assertEqual(r.status_code, 200, getattr(r, 'data', r.content))


class DeploymentGateOutboxDeliveryTests(DeployedEvidenceGateTests):
    """PLS-521 P2: fail-loud outbox delivery — a HELD Merge->Verification gate
    DMs the item owner/assignee, `published_at` is the idempotent delivery
    authority, and a delivery-boundary failure is loud + never marks published.
    """

    def _hold_outbox(self):
        # The item's created_by_id (self.user) is the resolvable owner recipient;
        # assignee is left unset to avoid the org-membership assignment guard.
        item = self._item()
        self._live_manifest(item)  # no evidence -> HOLD evidence_unreachable
        decision = evaluate_and_record_gate(
            item=item, transition=self.transition, actor=self.user)
        self.assertEqual(decision.outcome, 'hold')
        outbox = DeploymentEvidenceGateOutbox.objects.get(verdict=decision.verdict)
        self.assertIsNone(outbox.published_at)
        return item, outbox

    def test_hold_dms_owner_and_publishes(self):
        from tasks.tasks import deliver_deployment_gate_outbox
        item, outbox = self._hold_outbox()
        with mock.patch('tasks.tasks._send_gate_owner_dm') as send:
            deliver_deployment_gate_outbox.delay(str(outbox.id))
        self.assertTrue(send.called)
        self.assertIn(self.user.id, {c.args[0] for c in send.call_args_list})
        outbox.refresh_from_db()
        self.assertIsNotNone(outbox.published_at)

    def test_delivery_is_idempotent_on_published_row(self):
        from tasks.tasks import deliver_deployment_gate_outbox
        item, outbox = self._hold_outbox()
        outbox.published_at = timezone.now()
        outbox.save(update_fields=['published_at'])
        with mock.patch('tasks.tasks._send_gate_owner_dm') as send:
            deliver_deployment_gate_outbox.delay(str(outbox.id))
        self.assertFalse(send.called)  # no re-DM once published

    def test_concurrent_worker_coalesces_on_durable_in_flight_claim(self):
        from tasks.tasks import deliver_deployment_gate_outbox
        item, outbox = self._hold_outbox()
        concurrent_results = []

        def send_while_first_worker_owns_claim(_uid, _content):
            concurrent_results.append(
                deliver_deployment_gate_outbox.run(str(outbox.id)),
            )

        with mock.patch(
            'tasks.tasks._deployment_gate_recipients',
            return_value={self.user.id: 'owner'},
        ), mock.patch(
            'tasks.tasks._send_gate_owner_dm',
            side_effect=send_while_first_worker_owns_claim,
        ) as send:
            first = deliver_deployment_gate_outbox.run(str(outbox.id))

        self.assertEqual(first['status'], 'delivered')
        self.assertEqual(concurrent_results, [{
            'status': 'in_flight', 'outbox_id': str(outbox.id),
        }])
        self.assertEqual(send.call_count, 1)
        outbox.refresh_from_db()
        self.assertIsNotNone(outbox.published_at)
        self.assertFalse(outbox.in_flight)

    def test_hold_without_resolvable_recipient_is_not_published(self):
        from tasks.tasks import deliver_deployment_gate_outbox
        item, outbox = self._hold_outbox()
        with mock.patch(
            'tasks.tasks._deployment_gate_recipients', return_value={},
        ), mock.patch('tasks.tasks._send_gate_owner_dm') as send:
            deliver_deployment_gate_outbox.delay(str(outbox.id))

        self.assertFalse(send.called)
        outbox.refresh_from_db()
        self.assertIsNone(outbox.published_at)
        self.assertFalse(outbox.in_flight)
        self.assertIn('no_resolvable_recipient', outbox.last_error)

    def test_delivery_failure_is_loud_and_not_published(self):
        from tasks.tasks import deliver_deployment_gate_outbox
        item, outbox = self._hold_outbox()
        with mock.patch('tasks.tasks._send_gate_owner_dm',
                        side_effect=RuntimeError('connect down')):
            deliver_deployment_gate_outbox.delay(str(outbox.id))
        outbox.refresh_from_db()
        self.assertIsNone(outbox.published_at)  # a failed delivery never publishes

    def test_pass_verdict_publishes_without_dm(self):
        from tasks.tasks import deliver_deployment_gate_outbox
        item = self._item()
        self._source_manifest(item)  # source_only -> pass
        decision = evaluate_and_record_gate(
            item=item, transition=self.transition, actor=self.user)
        self.assertTrue(decision.passed)
        outbox = DeploymentEvidenceGateOutbox.objects.get(verdict=decision.verdict)
        with mock.patch('tasks.tasks._send_gate_owner_dm') as send:
            deliver_deployment_gate_outbox.delay(str(outbox.id))
        self.assertFalse(send.called)  # a pass verdict raises no owner DM
        outbox.refresh_from_db()
        self.assertIsNotNone(outbox.published_at)


class MergeEventRequirementManifestRoutingTests(TestCase):
    """PLAT-7066 (Retro 27Aug): merge lane must not retry on requirement_manifest_invalid.

    A merged security-finding cannot advance awaiting_merge → verification
    until Security freezes a VerificationRequirementManifest (PLS-521 gate).
    When the manifest is missing the transition returns 400
    requirement_manifest_invalid — a Security-side issue, NOT a merge action.
    The merge lane must record the pending marker, route to Security, and never
    arm retry backoff.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='plat7066', email='plat7066@shizuha.com', password='x',
            is_staff=True,
        )
        self.project = Project.objects.create(
            name='Pulse Gate', key='P7066', org_id=1, access_level='private',
        )
        awaiting_merge, _ = Status.objects.get_or_create(
            slug='awaiting_merge', defaults={
                'name': 'Awaiting Merge', 'category': 'in_progress', 'position': 1,
            },
        )
        verification, _ = Status.objects.get_or_create(
            slug='verification', defaults={
                'name': 'Awaiting Verification', 'category': 'in_progress', 'position': 2,
            },
        )
        self.workflow, _ = Workflow.objects.get_or_create(
            slug='security-finding', defaults={
                'name': 'Security Finding', 'initial_status': awaiting_merge,
            },
        )
        WorkflowStatus.objects.get_or_create(
            workflow=self.workflow, status=awaiting_merge, defaults={'position': 1},
        )
        WorkflowStatus.objects.get_or_create(
            workflow=self.workflow, status=verification, defaults={'position': 2},
        )
        self.transition = WorkflowTransition.objects.filter(
            workflow=self.workflow,
            from_status='awaiting_merge',
            to_status='verification',
        ).first()
        if not self.transition:
            self.transition = WorkflowTransition.objects.create(
                workflow=self.workflow,
                name='Merge',
                from_status='awaiting_merge',
                to_status='verification',
            )
        validators = list(self.transition.validators or [])
        if not any(
            isinstance(validator, dict)
            and validator.get('type') == 'deployment_evidence_gate'
            for validator in validators
        ):
            validators.append({'type': 'deployment_evidence_gate'})
            WorkflowTransition.objects.filter(pk=self.transition.pk).update(
                validators=validators,
            )
            self.transition.refresh_from_db()

    def _item(self, *, merged=True, pending_marker=False):
        metadata = {'pull_requests': [{
            'url': 'https://origin.shizuha.com/shizuha-labs/x/pulls/1',
            'status': 'merged' if merged else 'open',
            'head_sha': 'f' * 40,
            'merge_commit_sha': MERGE_COMMIT if merged else '',
            'merged_at': timezone.now().isoformat() if merged else None,
        }]}
        if pending_marker:
            metadata['merge_event_requirement_manifest_pending'] = True
        item = Item.objects.create(
            project=self.project,
            item_key=f'P7066-{Item.objects.filter(project=self.project).count() + 1}',
            title='Security finding',
            description='Exact deployed evidence required.',
            mode='task',
            item_type='task.security',
            labels=['service-affecting'],
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            workflow_name='security-finding',
            assignment_group='merge',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
            metadata=metadata,
        )
        if merged:
            record_merge_attestation(
                item=item,
                provider='forgejo',
                repo='shizuha-labs/x',
                number=1,
                url=metadata['pull_requests'][0]['url'],
                head_sha='f' * 40,
                merge_commit=MERGE_COMMIT,
                merged_at=metadata['pull_requests'][0]['merged_at'],
            )
        return item

    def test_apply_records_pending_marker_and_routes_to_security(self):
        """apply_merge_event_transition on requirement_manifest_invalid records
        the pending marker + ROUTED TO SECURITY comment (not a generic retry)."""
        item = self._item()
        with mock.patch.object(
            WorkflowService, '_verify_live_pr_merge_state',
            return_value=('proceed', 'test-stub'),
        ):
            target = WorkflowService.apply_merge_event_transition(
                item, user=None,
                pr_html_url='https://origin.shizuha.com/shizuha-labs/x/pulls/1',
                merged_by='revi',
            )
        self.assertIsNone(target)
        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')
        self.assertTrue(
            item.metadata.get('merge_event_requirement_manifest_pending')
        )
        routed = Comment.objects.filter(
            item=item,
            author_email='system@shizuha.com',
            content__contains='ROUTED TO SECURITY',
        )
        self.assertTrue(routed.exists())
        # No generic PLS-675 retry-failure comment is written for this class.
        self.assertFalse(
            Comment.objects.filter(
                item=item,
                content__contains='PLS-675 merge-event auto-transition FAILED',
            ).exists()
        )

    def test_attempt_retry_early_returns_and_mints_security_task(self):
        """_attempt_merge_event_retry on a marked item routes to Security
        (typed receipt + idempotent Security task + alert) and returns False —
        it never arms retry backoff."""
        item = self._item(pending_marker=True)
        with mock.patch('tasks.tasks.send_alert_notification.delay'):
            result = _attempt_merge_event_retry(item)
        self.assertFalse(result)
        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')
        # Typed receipt.
        self.assertTrue(
            Comment.objects.filter(
                item=item,
                origin_event='receipt.merge_event_requirement_manifest_pending',
            ).exists()
        )
        # Idempotent Security task to freeze the manifest.
        sec_task = Item.objects.filter(
            mode='task',
            metadata__merge_event_requirement_manifest_freeze_for=item.item_key,
        )
        self.assertEqual(sec_task.count(), 1)
        self.assertEqual(sec_task.first().assignment_group, 'security')
        # Blocking link parent -> security task.
        self.assertTrue(
            ItemLink.objects.filter(
                source_item=item, target_item=sec_task.first(),
            ).exists()
        )
        # Running again does not mint a duplicate Security task.
        with mock.patch('tasks.tasks.send_alert_notification.delay'):
            _attempt_merge_event_retry(item)
        self.assertEqual(
            Item.objects.filter(
                mode='task',
                metadata__merge_event_requirement_manifest_freeze_for=item.item_key,
            ).count(),
            1,
        )

    def test_drain_skips_security_routed_items(self):
        """_drain_merged_awaiting_merge_retries never retries an item marked
        merge_event_requirement_manifest_pending."""
        item = self._item(pending_marker=True)
        with mock.patch('tasks.tasks.send_alert_notification.delay'):
            attempted = _drain_merged_awaiting_merge_retries(10)
        self.assertEqual(attempted, 0)
        item.refresh_from_db()
        self.assertEqual(item.status, 'awaiting_merge')
        # No generic retry-pending receipt was written (not armed for retry).
        self.assertFalse(
            Comment.objects.filter(
                item=item,
                origin_event='receipt.merge_event_retry_pending',
            ).exists()
        )


class PLS1375LegacyComponentsAliasTests(DeployedEvidenceAuthorityTests):
    """PLS-1375 (PLAT-7689/7324): the verification-requirements create endpoint
    must (1) accept the legacy `components` alias exactly as the canonical
    field, (2) reject ambiguity/malformed input with field-naming 400s, and
    (3) convert service-level fail-closed ValidationError into a typed 400 —
    never a middleware 500 — while Security authority stays exact."""

    def _legacy_payload(self):
        payload = self._manifest_payload()
        payload['components'] = payload.pop('affected_components')
        return payload

    # ---- (1) legacy alias == canonical behavior ----
    def test_legacy_components_alias_freezes_201(self):
        r = self._post(self.security_member, self.MANIFEST_URL, self._legacy_payload())
        self.assertEqual(r.status_code, 201, getattr(r, 'data', r.content))
        self.assertEqual(r.data['affected_components'], ['source:tasks/foo.py'])
        # fail-closed derivation ran on the aliased set and classified it
        self.assertEqual(r.data['classification'], 'source_only')
        # frozen manifest is real and current for the item
        self.assertTrue(
            VerificationRequirementManifest.objects.filter(
                item=self.gate_item, classification='source_only',
            ).exists()
        )

    def test_legacy_alias_round_trips_through_get(self):
        created = self._post(self.security_member, self.MANIFEST_URL, self._legacy_payload())
        self.assertEqual(created.status_code, 201, created.data)
        client = APIClient()
        client.force_authenticate(self.security_member)
        with mock.patch(
            'tasks.views.PermissionService.get_user_item_queryset',
            return_value=Item.objects.all(),
        ):
            fetched = client.get(f"{self.MANIFEST_URL}{created.data['id']}/")
        self.assertEqual(fetched.status_code, 200, fetched.data)
        self.assertEqual(fetched.data['affected_components'], ['source:tasks/foo.py'])

    # ---- (2) both keys is ambiguous -> field-naming 400 ----
    def test_both_keys_rejected_naming_field(self):
        payload = self._manifest_payload()
        payload['components'] = list(payload['affected_components'])
        r = self._post(self.security_member, self.MANIFEST_URL, payload)
        self.assertEqual(r.status_code, 400, getattr(r, 'data', r.content))
        self.assertIn('affected_components', r.data)

    # ---- (3) absent/empty/untyped/wrong-type -> typed 400, never 500 ----
    def test_empty_and_absent_components_return_typed_400(self):
        base = self._manifest_payload()
        for label, payload in [
            ('absent-both', {k: v for k, v in base.items()
                             if k != 'affected_components'}),
            ('empty', {**base, 'affected_components': []}),
            ('empty-legacy', {**self._legacy_payload(), 'components': []}),
        ]:
            with self.subTest(case=label):
                r = self._post(self.security_member, self.MANIFEST_URL, payload)
                self.assertEqual(r.status_code, 400, getattr(r, 'data', r.content))
                self.assertIn('affected_components', r.data)

    def test_untyped_component_prefix_rejected_as_400(self):
        payload = self._legacy_payload()
        payload['components'] = ['tasks/foo.py']  # no typed source:/service: prefix
        r = self._post(self.security_member, self.MANIFEST_URL, payload)
        self.assertEqual(r.status_code, 400, getattr(r, 'data', r.content))
        self.assertIn('affected_components', r.data)

    def test_wrong_type_components_rejected_as_400(self):
        payload = self._legacy_payload()
        payload['components'] = 'source:tasks/foo.py'  # string, not list
        r = self._post(self.security_member, self.MANIFEST_URL, payload)
        self.assertEqual(r.status_code, 400, getattr(r, 'data', r.content))
        self.assertIn('affected_components', r.data)

    # ---- (4) service-level failure -> field-naming 400, no manifest written ----
    def test_missing_attestation_returns_400_without_manifest(self):
        item = self._item(merged=False)  # no provider merge attestation
        payload = self._manifest_payload()
        payload['components'] = payload.pop('affected_components')
        payload['item'] = item.pk
        before = VerificationRequirementManifest.objects.filter(item=item).count()
        r = self._post(self.security_member, self.MANIFEST_URL, payload)
        self.assertEqual(r.status_code, 400, getattr(r, 'data', r.content))
        self.assertIn('merge_commit', r.data)
        self.assertEqual(
            VerificationRequirementManifest.objects.filter(item=item).count(),
            before,
            'service failure must not create or supersede a manifest',
        )

    # ---- (5) legacy input cannot bypass Security authority ----
    def test_staff_nonmember_legacy_payload_still_403(self):
        r = self._post(self.staff_nonmember, self.MANIFEST_URL, self._legacy_payload())
        self.assertEqual(r.status_code, 403, getattr(r, 'data', r.content))
