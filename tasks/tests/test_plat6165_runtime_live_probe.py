"""PLAT-6165 workload-identity typed live-probe producer regressions."""

from unittest import mock

from django.test import TestCase, override_settings

from tasks.models import Comment
from tasks.services.workflow_service import WorkflowService
from tasks.tests.test_plat1351_deploy_verify_gate import (
    DeployVerifyGateV4Tests as _FactoryBase, MERGED_SHA,
)


@override_settings(ROOT_URLCONF='taskmanager.probe_urls', FORCE_SCRIPT_NAME=None)
class RuntimeLiveProbeWorkloadIdentityTests(TestCase):
    """PLAT-6165 private runtime attestation producer and negative controls."""

    _merged_pr = _FactoryBase._merged_pr
    _item = _FactoryBase._item
    _comment = _FactoryBase._comment
    _deploy_event = _FactoryBase._deploy_event
    _evidence = (
        'fresh_runtime=passed geometry=120x40 command=shizuha--help '
        'probe=plat6165-test output_bytes=42'
    )

    def setUp(self):
        _FactoryBase.setUp(self)
        from tasks.probe_views import _reset_probe_state_for_tests
        _reset_probe_state_for_tests()

    @staticmethod
    def _runtime_token():
        import base64
        import json
        import time

        now = int(time.time())
        payload = {
            'iat': now,
            'exp': now + 600,
            'aud': ['shizuha-pulse-runtime-evidence'],
            'kubernetes.io': {
                'namespace': 'rt-fleet',
                'serviceaccount': {'name': 'shizuha-runtime-fleet', 'uid': 'sa-uid'},
                'pod': {'name': 'shizuha-runtime-fleet-1', 'uid': 'pod-uid'},
            },
        }

        def encode(value):
            raw = json.dumps(value, separators=(',', ':')).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()

        return f'{encode({"alg": "none"})}.{encode(payload)}.signature'

    def _post_runtime_receipt(self, item, deployment_event, *, token=None):
        return self.client.post(
            '/api/internal/runtime/live-probe-receipt/',
            data={
                'item_key': item.item_key,
                'deployment_event_id': str(deployment_event.pk),
                'evidence': self._evidence,
            },
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {token or self._runtime_token()}',
            REMOTE_ADDR='10.42.1.55',
        )

    def _runtime_event(self, item, **overrides):
        extra_metadata = dict(overrides.pop('extra_metadata', {}) or {})
        extra_metadata.setdefault('origin_pr_number', 1)
        extra_metadata.setdefault(
            'fresh_runtime_evidence', self._evidence,
        )
        extra_metadata.setdefault('runtime_image_digest', 'sha256:' + 'a' * 64)
        extra_metadata.setdefault('desired_replicas', 1)
        extra_metadata.setdefault('ready_replicas', 1)
        extra_metadata.setdefault('available_replicas', 1)
        extra_metadata.setdefault('updated_replicas', 1)
        extra_metadata.setdefault('workload_generation', 7)
        extra_metadata.setdefault('observed_generation', 7)
        event = self._deploy_event(
            item,
            image='localhost:30500/shizuha-agent-runtime@sha256:' + 'a' * 64,
            image_digest='sha256:' + 'a' * 64,
            extra_metadata=extra_metadata,
            **overrides,
        )
        event.pr_numbers = [1]
        event.save(update_fields=['pr_numbers'])
        return event

    @mock.patch('tasks.probe_views._token_review')
    def test_exact_runtime_identity_mints_bound_immutable_typed_receipt(self, token_review):
        from tasks.probe_views import RUNTIME_EVIDENCE_AUDIENCE, RUNTIME_EVIDENCE_USERNAME

        token_review.return_value = {
            'authenticated': True,
            'audiences': [RUNTIME_EVIDENCE_AUDIENCE],
            'user': {'username': RUNTIME_EVIDENCE_USERNAME},
        }
        item = self._item(acceptance_mode='live', prs='merged')
        event = self._runtime_event(item, live_probe=False)

        response = self._post_runtime_receipt(item, event)

        self.assertEqual(response.status_code, 201, response.content)
        receipt = Comment.objects.get(pk=response.json()['receipt_id'])
        self.assertEqual(receipt.origin_event, 'receipt.live_probe_passed')
        self.assertEqual(receipt.origin_payload['issuer_team'], 'qa-automation')
        self.assertEqual(receipt.origin_payload['deployment_event_id'], str(event.pk))
        self.assertEqual(receipt.origin_payload['deployment_commit_sha'], MERGED_SHA)
        self.assertEqual(
            receipt.origin_payload['workload_identity'], RUNTIME_EVIDENCE_USERNAME,
        )
        self.assertTrue(WorkflowService._item_has_typed_live_probe(item, event))
        with self.assertRaises(Exception):
            receipt.origin_payload = {'result': 'forged'}
            receipt.save()

    @mock.patch('tasks.probe_views._token_review')
    def test_wrong_identity_and_mismatched_deployment_are_rejected(self, token_review):
        from tasks.probe_views import RUNTIME_EVIDENCE_AUDIENCE

        item = self._item(acceptance_mode='live', prs='merged')
        event = self._runtime_event(item, live_probe=False)
        token_review.return_value = {
            'authenticated': True,
            'audiences': [RUNTIME_EVIDENCE_AUDIENCE],
            'user': {'username': 'system:serviceaccount:rt-fleet:someone-else'},
        }
        denied = self._post_runtime_receipt(item, event)
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(item.comments.filter(origin_event='receipt.live_probe_passed').exists())

        token_review.return_value = {
            'authenticated': True,
            'audiences': [RUNTIME_EVIDENCE_AUDIENCE],
            'user': {'username': 'system:serviceaccount:rt-fleet:shizuha-runtime-fleet'},
        }
        wrong = self._runtime_event(
            item, commit_sha='f' * 40, source_task_keys=[item.item_key],
        )
        refused = self._post_runtime_receipt(item, wrong)
        self.assertEqual(refused.status_code, 403)
        self.assertIn('does not match a merged head', refused.json()['error'])

        other = self._item(acceptance_mode='live', prs='merged')
        mismatched = self._post_runtime_receipt(other, event)
        self.assertEqual(mismatched.status_code, 403)
        self.assertIn('does not name this task', mismatched.json()['error'])

    @mock.patch('tasks.probe_views._token_review')
    def test_nonconverged_deployment_and_free_form_comment_never_qualify(self, token_review):
        from tasks.probe_views import RUNTIME_EVIDENCE_AUDIENCE, RUNTIME_EVIDENCE_USERNAME

        token_review.return_value = {
            'authenticated': True,
            'audiences': [RUNTIME_EVIDENCE_AUDIENCE],
            'user': {'username': RUNTIME_EVIDENCE_USERNAME},
        }
        item = self._item(acceptance_mode='live', prs='merged')
        event = self._runtime_event(
            item,
            replicas_ready=False,
            extra_metadata={
                'desired_replicas': 1,
                'ready_replicas': 0,
                'available_replicas': 0,
                'updated_replicas': 0,
                'workload_generation': 7,
                'observed_generation': 7,
            },
            live_probe=False,
        )
        refused = self._post_runtime_receipt(item, event)
        self.assertEqual(refused.status_code, 403)
        self.assertIn('serving-replica convergence', refused.json()['error'])

        converged = self._runtime_event(item, live_probe=False)
        forged_evidence = self.client.post(
            '/api/internal/runtime/live-probe-receipt/',
            data={
                'item_key': item.item_key,
                'deployment_event_id': str(converged.pk),
                'evidence': 'forged ordinary text',
            },
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {self._runtime_token()}',
            REMOTE_ADDR='10.42.1.55',
        )
        self.assertEqual(forged_evidence.status_code, 403)
        self.assertIn('evidence does not match', forged_evidence.json()['error'])

        self._comment(
            item,
            'receipt.live_probe_passed issuer_team=qa-automation result=passed',
        )
        self.assertFalse(WorkflowService._item_has_typed_live_probe(item, event))

    @mock.patch('tasks.probe_views._token_review')
    def test_generic_ready_claim_cannot_replace_exact_runtime_convergence(
        self, token_review,
    ):
        from tasks.probe_views import RUNTIME_EVIDENCE_AUDIENCE, RUNTIME_EVIDENCE_USERNAME

        token_review.return_value = {
            'authenticated': True,
            'audiences': [RUNTIME_EVIDENCE_AUDIENCE],
            'user': {'username': RUNTIME_EVIDENCE_USERNAME},
        }
        item = self._item(acceptance_mode='live', prs='merged')
        event = self._runtime_event(
            item,
            extra_metadata={
                'runtime_image_digest': 'sha256:' + 'a' * 64,
                'desired_replicas': 1,
                'ready_replicas': 1,
                'available_replicas': 1,
                'updated_replicas': 0,
                'workload_generation': 7,
                'observed_generation': 7,
            },
            live_probe=False,
        )
        refused = self._post_runtime_receipt(item, event)
        self.assertEqual(refused.status_code, 403)
        self.assertIn('convergence is not exact', refused.json()['error'])
