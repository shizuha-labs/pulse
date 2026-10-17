"""PLAT-9602/9603: first-class FAIL-verdict class in the deploy-verify gate.

A legitimate, evidence-backed FAIL disposition (live verification ran and
failed) must terminal-close through the gate directly — same trust class as
the waiver/live-probe receipts, evidence equivalent to PASS. SimpleTestCase:
the seat has no Postgres; the DB-backed suites run in CI.
"""
from unittest import mock

from django.test import SimpleTestCase

from tasks.services import workflow_service
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


class _FakeComment:
    def __init__(self, payload, caused_by='qa-1@agents.shizuha.io'):
        self.origin_payload = payload
        self.caused_by_email_snapshot = caused_by


class _FakeCommentManager:
    def __init__(self, comments):
        self._comments = comments

    def filter(self, **kwargs):
        return list(self._comments)


class _FakeItem:
    def __init__(self, comments, item_key='PLAT-X', pk=1):
        self.comments = _FakeCommentManager(comments)
        self.item_key = item_key
        self.pk = pk


def _good_payload(caused='qa-1@agents.shizuha.io'):
    return {
        'result': 'failed',
        'verdict_state': 'live_probe_failed',
        'issuer_email': 'qa-1@agents.shizuha.io',
        'issuer_team': 'qa',
        'root_cause': 'upstream-503',
        'disposition_rationale': 'verified failing; fix tracked on child task',
        'deployment_event_id': '123',
    }


class HasTypedDeployVerifyFailedTests(SimpleTestCase):
    def test_accepts_well_formed_receipt(self):
        item = _FakeItem([_FakeComment(_good_payload())])
        self.assertTrue(WorkflowService._has_typed_deploy_verify_failed(item))

    def test_rejects_wrong_verdict_state(self):
        payload = _good_payload()
        payload['verdict_state'] = 'passed'
        self.assertFalse(
            WorkflowService._has_typed_deploy_verify_failed(_FakeItem([_FakeComment(payload)])))

    def test_rejects_automation_issuer(self):
        payload = _good_payload()
        payload['issuer_email'] = 'workflow@pulse'
        self.assertFalse(
            WorkflowService._has_typed_deploy_verify_failed(_FakeItem([_FakeComment(payload)])))

    def test_rejects_issuer_caused_by_mismatch(self):
        self.assertFalse(
            WorkflowService._has_typed_deploy_verify_failed(
                _FakeItem([_FakeComment(_good_payload(), caused_by='other@x.io')])))

    def test_rejects_missing_rationale(self):
        payload = _good_payload()
        payload['disposition_rationale'] = ''
        self.assertFalse(
            WorkflowService._has_typed_deploy_verify_failed(_FakeItem([_FakeComment(payload)])))

    def test_rejects_missing_event_ref(self):
        payload = _good_payload()
        del payload['deployment_event_id']
        self.assertFalse(
            WorkflowService._has_typed_deploy_verify_failed(_FakeItem([_FakeComment(payload)])))

    def test_rejects_non_dict_payload(self):
        self.assertFalse(
            WorkflowService._has_typed_deploy_verify_failed(
                _FakeItem([_FakeComment('not a dict')])))

    def test_false_on_scanner_exception(self):
        class Boom:
            def filter(self, **kw):
                raise RuntimeError('db down')
        item = _FakeItem([])
        item.comments = Boom()
        self.assertFalse(WorkflowService._has_typed_deploy_verify_failed(item))


class FailedVerdictCorrelationTests(SimpleTestCase):
    def test_matches_referenced_event(self):
        item = _FakeItem([_FakeComment(_good_payload())])
        ev = mock.Mock(spec=['id', 'pk'])
        ev.id, ev.pk = 123, 123
        self.assertTrue(WorkflowService._failed_verdict_correlates_event(item, ev))

    def test_does_not_match_other_event(self):
        item = _FakeItem([_FakeComment(_good_payload())])
        ev = mock.Mock(spec=['id', 'pk'])
        ev.id, ev.pk = 999, 999
        self.assertFalse(WorkflowService._failed_verdict_correlates_event(item, ev))


class _FakeActor:
    email = 'qa-1@agents.shizuha.io'


class _FakeDeployEvent:
    pk = 123
    id = 123
    commit_sha = 'a' * 40
    repo = 'shizuha-labs/cortex'
    image = 'registry/app@sha256:' + 'b' * 8
    image_digest = 'sha256:' + 'b' * 8
    image_tag = 'src-abc'
    state = 'smoke_failed'
    smoke_result = 'failed'


class MinterTests(SimpleTestCase):
    def _mint(self, item, **kw):
        return WorkflowService.issue_deploy_verify_failed_receipt(
            item,
            actor=_FakeActor(),
            verdict_state='live_probe_failed',
            root_cause='upstream-503',
            disposition_rationale='verified failing; child task tracks the fix',
            **kw)

    def test_rejects_bad_verdict_state(self):
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.issue_deploy_verify_failed_receipt(
                _FakeItem([]), actor=_FakeActor(), verdict_state='passed',
                root_cause='x', disposition_rationale='y')

    def test_rejects_automation_actor(self):
        bad = mock.Mock()
        bad.email = 'workflow@pulse'
        with mock.patch.object(WorkflowService, '_is_team_member', return_value=True):
            with self.assertRaises(Exception):
                WorkflowService.issue_deploy_verify_failed_receipt(
                    _FakeItem([]), actor=bad, verdict_state='qa_failed',
                    root_cause='x', disposition_rationale='y')

    def test_rejects_empty_rationale(self):
        with self.assertRaises(Exception):
            WorkflowService.issue_deploy_verify_failed_receipt(
                _FakeItem([]), actor=_FakeActor(), verdict_state='qa_failed',
                root_cause='x', disposition_rationale='  ')

    def test_mint_snapshots_correlated_event(self):
        item = _FakeItem([])
        with mock.patch.object(WorkflowService, '_is_team_member', return_value=True), \
             mock.patch.object(WorkflowService, '_merged_pr_heads',
                               return_value=[{'sha': 'a' * 40, 'repo': 'shizuha-labs/cortex'}]), \
             mock.patch.object(WorkflowService, '_find_correlated_deploy_event',
                               return_value=_FakeDeployEvent()), \
             mock.patch.object(WorkflowService, '_event_has_image', return_value=True), \
             mock.patch.object(WorkflowService, '_event_replicas_ready', return_value=True), \
             mock.patch('tasks.services.mechanical_comments.MechanicalCommentFactory') as mcf:
            mcf.create.return_value = mock.Mock()
            self._mint(item)
            kwargs = mcf.create.call_args.kwargs
            self.assertEqual(kwargs['origin_event'], 'receipt.deploy_verify_failed')
            payload = kwargs['structured_payload']
            self.assertEqual(payload['result'], 'failed')
            self.assertEqual(payload['verdict_state'], 'live_probe_failed')
            self.assertEqual(payload['issuer_email'], 'qa-1@agents.shizuha.io')
            self.assertTrue(payload['root_cause'])
            self.assertTrue(payload['disposition_rationale'])
            self.assertEqual(payload['deployment_event_id'], '123')
            self.assertEqual(
                payload['schema'], WorkflowService._DEPLOY_VERIFY_FAILED_SCHEMA)

    def test_mint_refused_without_correlated_deploy(self):
        item = _FakeItem([])
        with mock.patch.object(WorkflowService, '_is_team_member', return_value=True), \
             mock.patch.object(WorkflowService, '_merged_pr_heads',
                               return_value=[{'sha': 'a' * 40, 'repo': 'r'}]), \
             mock.patch.object(WorkflowService, '_find_correlated_deploy_event',
                               return_value=None):
            with self.assertRaises(Exception):
                self._mint(item)


class GateResultTests(SimpleTestCase):
    def test_fail_verdict_passes_gate_directly(self):
        item = _FakeItem([])
        item.item_key = 'PLAT-9602-FAKE'
        recorded = {}
        with mock.patch.object(WorkflowService, '_resolve_acceptance_mode',
                               return_value=WorkflowService._ACCEPTANCE_MODE_LIVE), \
             mock.patch.object(WorkflowService, '_resolve_deploy_verify_evidence_path',
                               return_value='default'), \
             mock.patch.object(WorkflowService, '_collect_deploy_verify_missing',
                               return_value=[]), \
             mock.patch.object(WorkflowService, '_has_role_gated_deploy_waiver',
                               return_value=False), \
             mock.patch.object(WorkflowService, '_has_typed_deploy_verify_failed',
                               return_value=True), \
             mock.patch.object(WorkflowService, '_record_deploy_verify_gate_liveness',
                               side_effect=lambda *a, **kw: recorded.update(
                                   result=a[3] if len(a) > 3 else kw.get('result'))):
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertEqual(recorded.get('result'), 'pass_fail_verdict')

    def test_waiver_still_wins_result_label(self):
        item = _FakeItem([])
        item.item_key = 'PLAT-9602-FAKE'
        recorded = {}
        with mock.patch.object(WorkflowService, '_resolve_acceptance_mode',
                               return_value=WorkflowService._ACCEPTANCE_MODE_LIVE), \
             mock.patch.object(WorkflowService, '_resolve_deploy_verify_evidence_path',
                               return_value='default'), \
             mock.patch.object(WorkflowService, '_collect_deploy_verify_missing',
                               return_value=[]), \
             mock.patch.object(WorkflowService, '_has_role_gated_deploy_waiver',
                               return_value=True), \
             mock.patch.object(WorkflowService, '_has_typed_deploy_verify_failed',
                               return_value=True), \
             mock.patch.object(WorkflowService, '_record_deploy_verify_gate_liveness',
                               side_effect=lambda *a, **kw: recorded.update(
                                   result=a[3] if len(a) > 3 else kw.get('result'))):
            WorkflowService._validate_deploy_verify_gate(item, 'done')
        self.assertEqual(recorded.get('result'), 'pass_waiver')
