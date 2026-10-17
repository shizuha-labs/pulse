"""HIVE-1153 P1 #3 increment 3c: the ownership-writing commit fence revalidates the
carried substrate-capability receipt LOCALLY under the Item+Workflow row locks.

Exercises WorkflowService._revalidate_substrate_receipt_at_commit end to end against
a REAL workflow (so the Workflow row lock + rk2 recompute run), proving it HOLDs on
policy/stage drift, writer-generation drift, and expiry, holds clean on a fresh
matching receipt, and is a strict no-op unless mode==enforce.
"""
import time
from unittest import mock

from django.db import transaction
from django.test import TestCase

from tasks.models import Item, Project, Status, Workflow, WorkflowStatus, WorkflowTransition
from tasks.services import capability_evidence as ce
from tasks.services.workflow_service import WorkflowService

PGEN = {'producer_generation_id': 'gen-x', 'producer_generation_digest': 'sha256:dd', 'kid': 'k'}


class CommitReceiptRevalidationTests(TestCase):
    def setUp(self):
        self.open = Status.objects.create(name='CR Open', slug='cr_open', category='todo')
        self.prog = Status.objects.create(name='CR Prog', slug='cr_prog', category='in_progress')
        self.wf = Workflow.objects.create(name='CR WF', slug='cr-wf', initial_status=self.open)
        for pos, st in enumerate((self.open, self.prog)):
            WorkflowStatus.objects.create(workflow=self.wf, status=st, position=pos)
        self.t = WorkflowTransition.objects.create(
            workflow=self.wf, name='Start', from_status='cr_open', to_status='cr_prog',
            assignee_only=True)
        self.project = Project.objects.create(name='CRP', key='CRP', org_id=1, access_level='open')
        # Create WITHOUT an assignee (the org guard would reject an unauthorized
        # assignee at the create fence); set the committing seat + stage in-memory —
        # the revalidation helper is exercised directly, it reads the instance, not a
        # persisted row.
        self.item = Item.objects.create(
            mode='task', title='t', project=self.project, workflow_name='cr-wf')
        self.item.status = 'cr_prog'
        self.item.assignee_email = 'dev@shizuha.com'
        # the seat's substrate requirement, so the fence-recomputed required-caps
        # vector matches the receipt's {kubeconfig}.
        self.item.labels = {'needs:kubeconfig': True}

    def _rk(self):
        return WorkflowService._requirement_key_v2_for_item(self.item, 1, intent='assign')

    def _consistent_request_sha256(self, *, request_id='req-test', item_id=None):
        # HIVE-1153 P1 #2: the fence rebuilds the request with the receipt's own
        # request_id + the item's CURRENT context and re-hashes it. A fresh matching
        # receipt must carry the SAME hash, so compute it exactly as the fence does.
        req = ce.build_evidence_request(
            email='dev@shizuha.com', organization_id=1, team_slug='',
            requirement_key=self._rk(), required_capabilities=('kubeconfig',),
            item_id=self.item.pk if item_id is None else item_id,
            assignment_generation=0, request_id=request_id)
        return ce.request_sha256(req)

    def _set_high_water(self, **kw):
        # HIVE-1153 P1 #2 (HIVE-1174): the durable per-seat materialization high-water
        # the commit fence checks against the carried receipt.
        from tasks.models import SubstrateMaterializationHighWater as _HW
        kw.setdefault('ambiguous', False)
        _HW.objects.update_or_create(
            producer_generation_id=str(kw['producer_generation_id']),
            organization_id=int(kw['organization_id']),
            subject=str(kw['subject']).strip().lower(),
            team_slug=str(kw['team_slug'] or ''),
            defaults=dict(
                iat=int(kw['iat']),
                materialization_generation=str(kw['materialization_generation']),
                ambiguous=bool(kw['ambiguous'])))

    def _receipt(self, **over):
        base = dict(
            requirement_key=self._rk(),
            request_sha256=self._consistent_request_sha256(), assignment_generation=0,
            organization_id=1, team_slug='', subject='dev@shizuha.com',
            required_capabilities=('kubeconfig',), present_capabilities=frozenset({'kubeconfig'}),
            materialization_generation='mat-sha256:' + 'c' * 64,
            producer_generation_id='gen-x', producer_generation_digest='sha256:dd',
            jti='j', expires_at=int(time.time()) + 5,
            item_id=self.item.pk, request_id='req-test', iat=1000)
        base.update(over)
        self.item._substrate_capability_receipts = {'dev@shizuha.com': ce.SubstrateCapabilityReceipt(**base)}
        # By default seed a MATCHING, non-ambiguous high-water so the fresh case holds
        # clean; the drift tests HOLD at an earlier check, and the P1 #2 tests override
        # this row to exercise supersession/ambiguity.
        self._set_high_water(
            producer_generation_id=base['producer_generation_id'],
            organization_id=base['organization_id'], subject=base['subject'],
            team_slug=base['team_slug'], iat=base['iat'],
            materialization_generation=base['materialization_generation'])

    def _reval(self, **kw):
        with mock.patch.object(ce, 'get_substrate_cap_mode', return_value=ce.MODE_ENFORCE), \
             mock.patch.object(ce, '_pinned_producer_generation', return_value=PGEN):
            return WorkflowService._revalidate_substrate_receipt_at_commit(self.item, **kw)

    def test_fresh_matching_receipt_holds(self):
        self._receipt()
        self.assertIsNone(self._reval())

    def test_no_op_unless_enforce(self):
        self._receipt()
        with mock.patch.object(ce, 'get_substrate_cap_mode', return_value=ce.MODE_OFF):
            self.assertIsNone(WorkflowService._revalidate_substrate_receipt_at_commit(self.item))
        with mock.patch.object(ce, 'get_substrate_cap_mode', return_value=ce.MODE_SHADOW):
            self.assertIsNone(WorkflowService._revalidate_substrate_receipt_at_commit(self.item))

    def test_missing_receipt_for_owner_write_holds_closed(self):
        # akira P1(a): a NEW owner/transfer requiring runtime substrate must fail
        # CLOSED on a missing/wrong-seat receipt. (This test previously asserted the
        # fail-OPEN behavior akira flagged — absence must now HOLD.)
        self.item._substrate_capability_receipts = {'other@shizuha.com': object()}
        self.assertEqual(self._reval(is_owner_write=True), 'missing_substrate_receipt')

    def test_status_only_save_without_receipt_is_noop(self):
        # a status-only save RETAINING the same owner did not (re)authorize a seat,
        # so it needs no fresh proof even under enforce.
        self.item._substrate_capability_receipts = {}
        self.assertIsNone(self._reval(is_owner_write=False))

    def test_no_substrate_requirement_is_noop_without_receipt(self):
        # a seat/stage with no runtime-substrate requirement needs no receipt.
        self.item.labels = {}
        self.item._substrate_capability_receipts = {}
        self.assertIsNone(self._reval(is_owner_write=True))

    def test_policy_edit_between_selection_and_commit_holds(self):
        # Snapshot a receipt, THEN flip an authorizing transition field -> the rk2
        # recomputed under the Workflow lock no longer matches -> drift HOLD.
        self._receipt()
        self.t.assignee_only = False
        self.t.save()
        self.assertEqual(self._reval(), 'requirement_key_drift')

    def test_stage_drift_between_selection_and_commit_holds(self):
        # Receipt bound to stage cr_prog; item moves to cr_open before commit.
        self._receipt()
        self.item.status = 'cr_open'
        self.assertEqual(self._reval(), 'requirement_key_drift')

    def test_expired_receipt_holds(self):
        self._receipt(expires_at=int(time.time()) - 1)
        self.assertEqual(self._reval(), 'receipt_expired')

    def test_writer_generation_drift_holds(self):
        self._receipt(assignment_generation=99)  # current item generation is 0
        self.assertEqual(self._reval(), 'writer_generation_drift')

    def test_producer_tuple_rotation_holds(self):
        self._receipt(producer_generation_id='gen-old')
        self.assertEqual(self._reval(), 'producer_generation_drift')

    def test_assignment_group_change_between_selection_and_commit_holds(self):
        # akira P1 #b: a receipt bound to team-a must not survive an assignment-group
        # change to team-b (rk2 does not encode the routed team; the separate
        # team_slug comparison catches it).
        self._receipt(team_slug='team-a')
        self.item.assignment_group = 'team-b'
        self.assertEqual(self._reval(), 'team_slug_drift')

    def test_capability_addition_between_selection_and_commit_holds(self):
        # akira P1 #b: adding an instance capability (host-exec) after the receipt
        # was minted for {kubeconfig} invalidates it — instance metadata/label caps
        # are not in the workflow-policy envelope, so the rk2 digest does not move.
        self._receipt()
        self.item.labels = {'needs:kubeconfig': True, 'needs:host-exec': True}
        self.assertEqual(self._reval(), 'required_capabilities_drift')

    def test_cross_item_receipt_replay_holds(self):
        # akira P1 #2: a receipt minted for a DIFFERENT item must not authorize this
        # item's owner write — the exact item binding fails closed in the 5 s window.
        self._receipt(item_id=self.item.pk + 9999)
        self.assertEqual(self._reval(), 'request_item_drift')

    def test_request_sha256_tamper_holds(self):
        # akira P1 #2: item_id matches but the frozen request hash does not correspond
        # to the request the fence rebuilds (a swapped/forged request binding) -> HOLD.
        self._receipt(request_sha256='sha256:' + '0' * 64)
        self.assertEqual(self._reval(), 'request_sha256_drift')


class SubstrateAuthorizingWriteSignalTests(TestCase):
    """HIVE-1153 P1 #1(a) (akira): Item.save must treat a RETAINED-owner context/
    stage drift as an authorizing write for the substrate fence — not only an owner
    delta — so a seat moving INTO a substrate-required stage without a fresh receipt
    fails closed instead of slipping through as a status-only no-op."""

    def setUp(self):
        self.open = Status.objects.create(name='SA Open', slug='sa_open', category='todo')
        self.prog = Status.objects.create(name='SA Prog', slug='sa_prog', category='in_progress')
        self.wf = Workflow.objects.create(name='SA WF', slug='sa-wf', initial_status=self.open)
        for pos, st in enumerate((self.open, self.prog)):
            WorkflowStatus.objects.create(workflow=self.wf, status=st, position=pos)
        self.project = Project.objects.create(name='SAP', key='SAP', org_id=1, access_level='open')
        # dev@shizuha.com is a valid org-1 (shizuha) roster agent so the unguarded
        # assignee write passes assignment_org_mismatch (task-org membership).
        from tasks.models import TeamRoster
        TeamRoster.objects.create(
            team_name='eng', organization_slug='shizuha',
            tiers=[['dev@shizuha.com']], is_active=True)
        self.item = Item.objects.create(
            mode='task', title='t', project=self.project, workflow_name='sa-wf', status='sa_open')
        # Establish a retained owner on an UNGUARDED lane (no assignment_group). A
        # normal save sets the org-isolation proof the DB trigger requires; the later
        # status-only save (assignee unchanged) early-returns past that trigger, so it
        # exercises the substrate-fence authorizing-write signal cleanly.
        with mock.patch('tasks.services.admin_service.admin_service.get_organization',
                        side_effect=lambda o: {'id': 1, 'slug': 'shizuha'} if o == 1 else None), \
             mock.patch('tasks.services.admin_service.admin_service.is_org_member',
                        return_value=True):
            self.item.assignee_email = 'dev@shizuha.com'
            self.item.save()
        self.item.refresh_from_db()

    def _capture_is_owner_write(self, do_save):
        captured = {}

        def spy(it, *, is_owner_write=True, previous_assignee_email=None):
            captured['is_owner_write'] = is_owner_write
            return None

        with mock.patch.object(
                WorkflowService, '_revalidate_substrate_receipt_at_commit',
                side_effect=spy):
            do_save()
        return captured.get('is_owner_write')

    def test_retained_owner_stage_change_is_authorizing_write(self):
        def save_stage():
            self.item.status = 'sa_prog'
            self.item.save(update_fields=['status'])
        self.assertTrue(
            self._capture_is_owner_write(save_stage),
            'a retained-owner STAGE change must be an authorizing write for the '
            'substrate fence (fail-closed into a substrate-required stage)')

    def test_no_change_save_is_not_an_authorizing_write(self):
        # Re-saving the SAME status + owner is not a (re)authorization.
        def save_noop():
            self.item.save(update_fields=['status'])
        self.assertFalse(
            self._capture_is_owner_write(save_noop),
            'an idempotent same-stage/same-owner save must not require fresh proof')


class MaterializationHighWaterCommitTests(CommitReceiptRevalidationTests):
    """HIVE-1153 P1 #2 (HIVE-1174): the commit fence HOLDs when the carried receipt's
    materialization is no longer the durable per-seat high-water — a newer materialization
    verified inside the 5s window, an ambiguous seat, or a generation mismatch."""

    def test_old_receipt_holds_after_newer_materialization(self):
        # receipt bound to iat=1000; a NEWER materialization (iat=2000) advanced the
        # high-water since selection -> the carried receipt is superseded.
        self._receipt()
        self._set_high_water(
            producer_generation_id='gen-x', organization_id=1, subject='dev@shizuha.com',
            team_slug='', iat=2000, materialization_generation='mat-sha256:' + 'e' * 64)
        self.assertEqual(self._reval(), 'materialization_superseded')

    def test_ambiguous_seat_holds(self):
        self._receipt()
        self._set_high_water(
            producer_generation_id='gen-x', organization_id=1, subject='dev@shizuha.com',
            team_slug='', iat=1000, materialization_generation='mat-sha256:' + 'c' * 64,
            ambiguous=True)
        self.assertEqual(self._reval(), 'materialization_ambiguous')

    def test_generation_mismatch_holds(self):
        # same iat, different materialization generation on the row -> mismatch.
        self._receipt()
        self._set_high_water(
            producer_generation_id='gen-x', organization_id=1, subject='dev@shizuha.com',
            team_slug='', iat=1000, materialization_generation='mat-sha256:' + 'f' * 64)
        self.assertEqual(self._reval(), 'materialization_generation_mismatch')

    def test_missing_high_water_holds(self):
        self._receipt()
        from tasks.models import SubstrateMaterializationHighWater as _HW
        _HW.objects.all().delete()
        self.assertEqual(self._reval(), 'materialization_high_water_missing')


class MaterializationHighWaterAdvanceTests(TestCase):
    """HIVE-1153 P1 #2 (HIVE-1174): the verification-time monotonic advance of the durable
    per-seat high-water."""

    def _ev(self, *, iat, gen='mat-sha256:' + 'a' * 64, subject='dev@shizuha.com',
            team_slug='', gen_id='gen-x', org=1):
        return ce.VerifiedEvidence(
            subject=subject, organization_id=org, team_slug=team_slug,
            request_sha256='sha256:r', required_capabilities=('kubeconfig',),
            present_capabilities=frozenset({'kubeconfig'}), materialization_generation=gen,
            producer_generation_id=gen_id, jti='j', iat=iat)

    def test_newer_iat_advances_and_older_never_rewinds(self):
        self.assertIsNone(ce.advance_materialization_high_water(self._ev(iat=1000)))
        # a strictly-newer response advances
        self.assertIsNone(ce.advance_materialization_high_water(
            self._ev(iat=2000, gen='mat-sha256:' + 'b' * 64)))
        from tasks.models import SubstrateMaterializationHighWater as _HW
        self.assertEqual(_HW.objects.get(producer_generation_id='gen-x').iat, 2000)
        # a strictly-OLDER (out-of-order) response is already superseded -> HOLD, no rewind
        self.assertEqual(
            ce.advance_materialization_high_water(self._ev(iat=1500)),
            'materialization_superseded')
        self.assertEqual(_HW.objects.get(producer_generation_id='gen-x').iat, 2000)

    def test_equal_iat_conflicting_generation_latches_ambiguous(self):
        self.assertIsNone(ce.advance_materialization_high_water(
            self._ev(iat=1000, gen='mat-sha256:' + 'a' * 64)))
        # same iat, DIFFERENT generation -> ambiguity, fail closed
        self.assertEqual(
            ce.advance_materialization_high_water(self._ev(iat=1000, gen='mat-sha256:' + 'b' * 64)),
            'materialization_ambiguous')
        # the seat stays ambiguous until a strictly-newer iat resolves it
        self.assertEqual(
            ce.advance_materialization_high_water(self._ev(iat=1000, gen='mat-sha256:' + 'a' * 64)),
            'materialization_ambiguous')
        self.assertIsNone(ce.advance_materialization_high_water(
            self._ev(iat=2000, gen='mat-sha256:' + 'c' * 64)))

    def test_exact_seat_isolation(self):
        # a different team_slug is a different seat and does not interfere.
        self.assertIsNone(ce.advance_materialization_high_water(self._ev(iat=1000, team_slug='a')))
        self.assertIsNone(ce.advance_materialization_high_water(self._ev(iat=500, team_slug='b')))
        from tasks.models import SubstrateMaterializationHighWater as _HW
        self.assertEqual(_HW.objects.count(), 2)
