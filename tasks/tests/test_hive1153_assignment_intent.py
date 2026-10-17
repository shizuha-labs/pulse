"""HIVE-1153 P1(b).2 (HLD PLS-277 v20 §5.2 point 6, akira re-review): concrete
writer intent, resolved by ONE resolver and bound into rk2 at BOTH boundaries.

Before this, both the selection-time evidence request and the commit-time
revalidation hard-coded ``intent='assign'`` — so the intent binding was vacuous: a
receipt minted for any write authorized any other write type. Now
``WorkflowService._resolve_assignment_intent`` is the single source (read at both
boundaries), fed by the owner-writer's declared ``item._substrate_write_intent``, and
a receipt minted under one intent DRIFTS at a commit whose write is a different
intent.
"""
import time
import unittest
from unittest import mock

from django.test import TestCase

from tasks.models import Item, Project, Status, Workflow, WorkflowStatus, WorkflowTransition
from tasks.services import capability_evidence as ce
from tasks.services.workflow_service import WorkflowService

PGEN = {'producer_generation_id': 'gen-x', 'producer_generation_digest': 'sha256:dd', 'kid': 'k'}


class AssignmentIntentResolverTests(TestCase):
    def test_unset_flag_defaults_to_assign(self):
        item = type('I', (), {})()
        self.assertEqual(WorkflowService._resolve_assignment_intent(item), 'assign')

    def test_each_valid_intent_resolves_to_itself(self):
        for intent in sorted(ce.ASSIGNMENT_INTENTS):
            item = type('I', (), {'_substrate_write_intent': intent})()
            self.assertEqual(WorkflowService._resolve_assignment_intent(item), intent)

    def test_case_and_whitespace_normalized(self):
        item = type('I', (), {'_substrate_write_intent': '  TRANSFER '})()
        self.assertEqual(WorkflowService._resolve_assignment_intent(item), 'transfer')

    def test_unknown_declared_intent_fails_closed(self):
        item = type('I', (), {'_substrate_write_intent': 'sudo'})()
        with self.assertRaises(ValueError):
            WorkflowService._resolve_assignment_intent(item)


class CommitFenceIntentBindingTests(TestCase):
    """The fence recomputes rk2 with the item's CONCRETE write intent, so a receipt
    minted under a different intent is rejected as requirement_key drift."""

    def setUp(self):
        self.open = Status.objects.create(name='AI Open', slug='ai_open', category='todo')
        self.prog = Status.objects.create(name='AI Prog', slug='ai_prog', category='in_progress')
        self.wf = Workflow.objects.create(name='AI WF', slug='ai-wf', initial_status=self.open)
        for pos, st in enumerate((self.open, self.prog)):
            WorkflowStatus.objects.create(workflow=self.wf, status=st, position=pos)
        self.t = WorkflowTransition.objects.create(
            workflow=self.wf, name='Start', from_status='ai_open', to_status='ai_prog',
            assignee_only=True)
        self.project = Project.objects.create(name='AIP', key='AIP', org_id=1, access_level='open')
        self.item = Item.objects.create(
            mode='task', title='t', project=self.project, workflow_name='ai-wf')
        self.item.status = 'ai_prog'
        self.item.assignee_email = 'dev@shizuha.com'
        self.item.labels = {'needs:kubeconfig': True}

    def _rk(self, intent):
        return WorkflowService._requirement_key_v2_for_item(self.item, 1, intent=intent)

    def _receipt(self, rk):
        # HIVE-1153 P1 #2: bind to the exact item + request. request_sha256 is the
        # hash the fence rebuilds for THIS item under the receipt's rk (a matching
        # intent holds; an intent mismatch drifts at requirement_key first).
        req = ce.build_evidence_request(
            email='dev@shizuha.com', organization_id=1, team_slug='',
            requirement_key=rk, required_capabilities=('kubeconfig',),
            item_id=self.item.pk, assignment_generation=0, request_id='req-test')
        self.item._substrate_capability_receipts = {'dev@shizuha.com': ce.SubstrateCapabilityReceipt(
            requirement_key=rk, request_sha256=ce.request_sha256(req), assignment_generation=0,
            organization_id=1, team_slug='', subject='dev@shizuha.com',
            required_capabilities=('kubeconfig',), present_capabilities=frozenset({'kubeconfig'}),
            materialization_generation='mat-sha256:' + 'c' * 64,
            producer_generation_id='gen-x', producer_generation_digest='sha256:dd',
            jti='j', expires_at=int(time.time()) + 5,
            item_id=self.item.pk, request_id='req-test', iat=1000)}
        # HIVE-1153 P1 #2: the durable per-seat high-water the commit fence checks —
        # matching + non-ambiguous so an intent-matching receipt holds clean.
        from tasks.models import SubstrateMaterializationHighWater as _HW
        _HW.objects.update_or_create(
            producer_generation_id='gen-x', organization_id=1,
            subject='dev@shizuha.com', team_slug='',
            defaults=dict(iat=1000, materialization_generation='mat-sha256:' + 'c' * 64,
                          ambiguous=False))

    def _reval(self):
        with mock.patch.object(ce, 'get_substrate_cap_mode', return_value=ce.MODE_ENFORCE), \
             mock.patch.object(ce, '_pinned_producer_generation', return_value=PGEN):
            return WorkflowService._revalidate_substrate_receipt_at_commit(self.item)

    def test_matching_intent_holds_clean(self):
        # receipt minted for the SAME intent the write declares -> rk matches -> no drift
        self.item._substrate_write_intent = 'transfer'
        self._receipt(self._rk('transfer'))
        self.assertIsNone(self._reval())

    def test_selection_assign_commit_transfer_drifts(self):
        # receipt minted at selection under 'assign', but the committing write is a
        # transfer -> the fence recomputes rk2 under 'transfer' -> requirement_key drift
        self.item._substrate_write_intent = 'transfer'
        self._receipt(self._rk('assign'))
        self.assertEqual(self._reval(), 'requirement_key_drift')

    def test_selection_transfer_commit_default_assign_drifts(self):
        # a transfer-minted receipt cannot authorize a write that declares no intent
        # (defaults to 'assign')
        self._receipt(self._rk('transfer'))  # item._substrate_write_intent unset -> 'assign'
        self.assertEqual(self._reval(), 'requirement_key_drift')

    def test_resume_intent_binds(self):
        self.item._substrate_write_intent = 'resume'
        self._receipt(self._rk('resume'))
        self.assertIsNone(self._reval())
        # a resume-minted receipt drifts against a transfer commit
        self.item._substrate_write_intent = 'transfer'
        self.assertEqual(self._reval(), 'requirement_key_drift')


class PostFunctionTransferIntentTests(TestCase):
    """A transition post-function that (re)assigns the owner declares intent='transfer'
    on the item so the destination-stage owner's receipt binds correctly."""

    def setUp(self):
        self.open = Status.objects.create(name='PF Open', slug='pf_open', category='todo')
        self.done = Status.objects.create(name='PF Done', slug='pf_done', category='done')
        self.wf = Workflow.objects.create(name='PF WF', slug='pf-wf', initial_status=self.open)
        for pos, st in enumerate((self.open, self.done)):
            WorkflowStatus.objects.create(workflow=self.wf, status=st, position=pos)
        self.project = Project.objects.create(name='PFP', key='PFP', org_id=1, access_level='open')
        self.item = Item.objects.create(
            mode='task', title='t', project=self.project, workflow_name='pf-wf', status='pf_done')

    def test_assign_to_post_function_declares_transfer(self):
        # item.status is already the DESTINATION ('pf_done') at post-function time, so
        # the rk2 stage is the destination stage AND the intent is 'transfer'.
        t = WorkflowTransition.objects.create(
            workflow=self.wf, name='Finish', from_status='pf_open', to_status='pf_done',
            post_functions=[{'type': 'assign_to', 'target': 'reporter'}])
        WorkflowService.execute_post_functions(t, self.item)
        self.assertEqual(getattr(self.item, '_substrate_write_intent', None), 'transfer')
        self.assertEqual(WorkflowService._resolve_assignment_intent(self.item), 'transfer')


class SystemWriterIntentSourceScanTests(unittest.TestCase):
    """HIVE-1153 P1 #1(b) (akira): every system-automation owner-reassign must DECLARE
    its concrete intent, so a sweep/failover receipt binds to 'system' and cannot be
    replayed to authorize a different-intent (assign/transfer) owner write. This scan
    fails closed if a NEW sweep reassign forgets to declare intent (regression guard).
    """

    def test_every_sweep_reassign_declares_system_intent(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        src = (root / 'tasks' / 'tasks.py').read_text().splitlines()
        offenders = []
        for i, line in enumerate(src):
            # The canonical rebalance/failover reassign form: X.assign({'id': to_uid,
            # 'email': to_email}, ...). Every one must set _substrate_write_intent on
            # the same receiver before the following save (within a small window).
            if '.assign({' in line and 'to_uid' in line and 'to_email' in line:
                window = '\n'.join(src[max(0, i - 2):i + 6])
                if '_substrate_write_intent' not in window:
                    offenders.append(f'tasks/tasks.py:{i + 1}: {line.strip()}')
        self.assertEqual(
            offenders, [],
            'these tasks.py sweep reassign sites do not declare a substrate write '
            'intent (HIVE-1153 P1 #1(b)); a system reassign must set '
            "_substrate_write_intent='system' so its receipt cannot authorize a "
            'different-intent write:\n' + '\n'.join(offenders))
