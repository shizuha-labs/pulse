"""PLS-824 / PLS-819 S1 — focused-QA disposition transaction core, production-order fixtures (HLD §8)."""
import uuid
from unittest.mock import patch

from django.test import TestCase

from tasks.models import (
    FocusedQABugEquivalenceKey,
    FocusedQADispositionReceipt,
    FocusedQAGeneration,
    Item,
    ItemLink,
    Project,
)
from tasks.services.focused_qa_disposition import (
    BUG_EQUIVALENCE_CONFLICT,
    IDEMPOTENCY_MISMATCH,
    ILLEGAL_DISPOSITION,
    DispositionOutcome,
    GenerationConflict,
    canonicalize_fingerprint,
    commit_disposition,
    failure_fingerprint,
    find_or_create_generation,
    origin_surface,
)


def _make_project(key='PLS'):
    return Project.objects.create(key=key, org_id=1, name=key)


def _make_origin(**kw):
    defaults = dict(title='Originating task', status='open', workflow_name='autonomous-bug')
    defaults.update(kw)
    return Item.objects.create(**defaults)


def _make_qa_task(project, origin):
    return Item.objects.create(
        project=project,
        title='Focused QA verification',
        status='in_review',
        workflow_name='reviewed-task',
        source='focused-qa-automation',
        source_id=str(origin.pk),
    )


def _make_generation(*, project, origin, qa_task, generation_key='gen-1', organization='shizuha',
                     environment='live', **kw):
    return find_or_create_generation(
        generation_key=generation_key,
        focused_qa_task=qa_task,
        originating_task=origin,
        organization=organization,
        environment=environment,
        origin_surface_schema='task-key.v1',
        origin_surface_key=str(origin.pk),
        owning_project=project,
        **kw,
    )


def _fail_payload(fingerprint='SYMPTOM-X'):
    return {
        'failure_fingerprint': failure_fingerprint(fingerprint),
        'summary': 'observed defect',
        'reproduction': 'live steps',
    }


class CanonicalizationTests(TestCase):
    def test_fingerprint_whitespace_and_nfc(self):
        self.assertEqual(canonicalize_fingerprint('  Foo   Bar\nBaz  '), 'Foo Bar Baz')
        composed = 'caf\u00e9'
        decomposed = 'cafe\u0301'
        self.assertEqual(canonicalize_fingerprint(decomposed), canonicalize_fingerprint(composed))


class GenerationIdempotencyTests(TestCase):
    def setUp(self):
        self.project = _make_project()
        self.origin = _make_origin(project=self.project)

    def _gen(self, key='gen-1', task=None, **kw):
        if task is None:
            task = _make_qa_task(self.project, self.origin)
        return _make_generation(project=self.project, origin=self.origin, qa_task=task, generation_key=key, **kw)

    def test_same_key_same_target_returns_existing(self):
        task = _make_qa_task(self.project, self.origin)
        g1 = self._gen(task=task)
        g2 = self._gen(task=task)
        self.assertEqual(g1.pk, g2.pk)
        self.assertEqual(FocusedQAGeneration.objects.count(), 1)

    def test_same_key_different_target_conflicts(self):
        task = _make_qa_task(self.project, self.origin)
        self._gen(key='g', task=task)
        with self.assertRaises(GenerationConflict):
            self._gen(key='g', task=task, environment='prod')

    def test_different_keys_create_distinct_generations(self):
        task = _make_qa_task(self.project, self.origin)
        g1 = self._gen('a', task=task)
        task2 = _make_qa_task(self.project, self.origin)
        g2 = self._gen('b', task=task2)
        self.assertNotEqual(g1.pk, g2.pk)

    def test_append_only_forbids_delete(self):
        task = _make_qa_task(self.project, self.origin)
        g1 = self._gen(task=task)
        with self.assertRaises(TypeError):
            g1.delete()


class DispositionReplayTests(TestCase):
    def setUp(self):
        self.project = _make_project()
        self.origin = _make_origin(project=self.project)
        self.qa = _make_qa_task(self.project, self.origin)
        self.gen = _make_generation(project=self.project, origin=self.origin, qa_task=self.qa)

    def test_pass_terminal_and_exact_replay(self):
        out1 = commit_disposition(
            task=self.qa, idempotency_key='k-pass', disposition='pass',
            screen_data={}, organization='shizuha',
        )
        self.assertFalse(out1.replayed)
        self.assertEqual(out1.result['resulting_status'], 'done')
        self.assertEqual(self.qa.status, 'done')
        self.assertEqual(FocusedQADispositionReceipt.objects.count(), 1)

        # Exact replay from done returns the stored receipt, no second receipt.
        out2 = commit_disposition(
            task=self.qa, idempotency_key='k-pass', disposition='pass',
            screen_data={}, organization='shizuha',
        )
        self.assertTrue(out2.replayed)
        self.assertEqual(out2.result['resulting_status'], 'done')
        self.assertEqual(FocusedQADispositionReceipt.objects.count(), 1)
        self.assertIsNone(out2.task)

    def test_idempotency_mismatch_after_fail(self):
        commit_disposition(
            task=self.qa, idempotency_key='k-fail', disposition='fail',
            screen_data=_fail_payload(), organization='shizuha',
        )
        out = commit_disposition(
            task=self.qa, idempotency_key='k-fail', disposition='fail',
            screen_data=_fail_payload('DIFFERENT'), organization='shizuha',
        )
        self.assertEqual(out.code, IDEMPOTENCY_MISMATCH)
        # no side effects / no new receipt
        self.assertEqual(FocusedQADispositionReceipt.objects.count(), 1)
        self.assertEqual(Item.objects.filter(item_type='bug.qa_failure').count(), 1)

    def test_fresh_key_after_terminal_gets_illegal_disposition(self):
        commit_disposition(
            task=self.qa, idempotency_key='k-pass', disposition='pass',
            screen_data={}, organization='shizuha',
        )
        out = commit_disposition(
            task=self.qa, idempotency_key='k-new', disposition='subsumed',
            screen_data={}, target_task=self.origin, organization='shizuha',
        )
        self.assertEqual(out.code, ILLEGAL_DISPOSITION)


class FailEquivalenceTests(TestCase):
    def setUp(self):
        self.project = _make_project()
        self.origin = _make_origin(project=self.project)
        self.qa = _make_qa_task(self.project, self.origin)
        self.gen = _make_generation(project=self.project, origin=self.origin, qa_task=self.qa)

    def test_fail_creates_one_bug_and_two_links(self):
        out = commit_disposition(
            task=self.qa, idempotency_key='k1', disposition='fail',
            screen_data=_fail_payload('SYM-A'), organization='shizuha',
        )
        self.assertFalse(out.replayed)
        self.assertEqual(self.qa.status, 'done')
        bugs = Item.objects.filter(item_type='bug.qa_failure')
        self.assertEqual(bugs.count(), 1)
        bug = bugs.first()
        self.assertEqual(out.result['product_bug']['id'], str(bug.pk))
        # both required link sets exactly once (qa→bug and origin→bug)
        links = ItemLink.objects.filter(
            source_item__in=[self.qa, self.origin], target_item=bug, link_type='relates_to',
        )
        self.assertEqual(links.count(), 2)
        # equivalence key row exists with full tuple
        eq = FocusedQABugEquivalenceKey.objects.get()
        self.assertEqual(eq.organization, 'shizuha')
        self.assertEqual(eq.canonical_failure_fingerprint_value, 'SYM-A')
        receipt = FocusedQADispositionReceipt.objects.get(disposition='fail')
        self.assertEqual(receipt.product_bug_id, bug.pk)
        self.assertEqual(receipt.equivalence_key_id, eq.pk)

    def test_concurrent_identical_fail_creates_one_bug(self):
        out1 = commit_disposition(
            task=self.qa, idempotency_key='race', disposition='fail',
            screen_data=_fail_payload('SYM-A'), organization='shizuha',
        )
        out2 = commit_disposition(
            task=self.qa, idempotency_key='race', disposition='fail',
            screen_data=_fail_payload('SYM-A'), organization='shizuha',
        )
        # loser replays / converges — single bug, single receipt
        self.assertTrue(out2.replayed or (out2.task and out2.task.pk))
        self.assertEqual(Item.objects.filter(item_type='bug.qa_failure').count(), 1)
        self.assertEqual(FocusedQADispositionReceipt.objects.filter(disposition='fail').count(), 1)

    def test_two_generations_same_equivalence_share_active_bug(self):
        qa2 = _make_qa_task(self.project, self.origin)
        gen2 = _make_generation(
            project=self.project, origin=self.origin, qa_task=qa2,
            generation_key='gen-2',
        )
        commit_disposition(
            task=self.qa, idempotency_key='k1', disposition='fail',
            screen_data=_fail_payload('SHARED'), organization='shizuha',
        )
        out2 = commit_disposition(
            task=qa2, idempotency_key='k2', disposition='fail',
            screen_data=_fail_payload('SHARED'), organization='shizuha',
        )
        self.assertFalse(out2.replayed)
        bugs = list(Item.objects.filter(item_type='bug.qa_failure'))
        self.assertEqual(len(bugs), 1)  # reused the single active owning bug
        self.assertEqual(
            FocusedQADispositionReceipt.objects.filter(disposition='fail').count(), 2,
        )
        # one equivalence key, both receipts reference it
        eq = FocusedQABugEquivalenceKey.objects.get()
        self.assertEqual(
            FocusedQADispositionReceipt.objects.filter(disposition='fail', equivalence_key=eq).count(), 2,
        )

    def test_different_fingerprint_gets_distinct_bug(self):
        commit_disposition(
            task=self.qa, idempotency_key='k1', disposition='fail',
            screen_data=_fail_payload('SYM-A'), organization='shizuha',
        )
        qa2 = _make_qa_task(self.project, self.origin)
        _make_generation(project=self.project, origin=self.origin, qa_task=qa2, generation_key='gen-2')
        commit_disposition(
            task=qa2, idempotency_key='k2', disposition='fail',
            screen_data=_fail_payload('SYM-B'), organization='shizuha',
        )
        self.assertEqual(Item.objects.filter(item_type='bug.qa_failure').count(), 2)
        self.assertEqual(FocusedQABugEquivalenceKey.objects.count(), 2)

    def test_different_surface_gets_distinct_bug(self):
        commit_disposition(
            task=self.qa, idempotency_key='k1', disposition='fail',
            screen_data=_fail_payload('SYM-A'), organization='shizuha',
        )
        qa2 = _make_qa_task(self.project, self.origin)
        find_or_create_generation(
            generation_key='gen-2', focused_qa_task=qa2, originating_task=self.origin,
            organization='shizuha', environment='live',
            origin_surface_schema='task-key.v1',
            origin_surface_key='OTHERSURFACE',
            owning_project=self.project,
        )
        commit_disposition(
            task=qa2, idempotency_key='k2', disposition='fail',
            screen_data=_fail_payload('SYM-A'), organization='shizuha',
        )
        self.assertEqual(Item.objects.filter(item_type='bug.qa_failure').count(), 2)
        self.assertEqual(FocusedQABugEquivalenceKey.objects.count(), 2)

    def test_non_fail_dispositions_create_zero_bugs(self):
        commit_disposition(
            task=self.qa, idempotency_key='k-c', disposition='correct',
            screen_data={}, organization='shizuha',
        )
        commit_disposition(
            task=self.qa, idempotency_key='k-s', disposition='subsumed',
            screen_data={}, target_task=self.origin, organization='shizuha',
        )
        self.assertEqual(Item.objects.filter(item_type='bug.qa_failure').count(), 0)
        self.assertEqual(FocusedQADispositionReceipt.objects.filter(disposition='fail').count(), 0)
        self.assertEqual(FocusedQABugEquivalenceKey.objects.count(), 0)


class RegressionTests(TestCase):
    def test_later_generation_after_terminal_bug_creates_regression(self):
        project = _make_project()
        origin = _make_origin(project=project)
        qa1 = _make_qa_task(project, origin)
        _make_generation(project=project, origin=origin, qa_task=qa1, generation_key='g1')
        commit_disposition(
            task=qa1, idempotency_key='k1', disposition='fail',
            screen_data=_fail_payload('RECUR'), organization='shizuha',
        )
        # terminalize the owning bug
        bug1 = Item.objects.get(item_type='bug.qa_failure')
        bug1.status = 'done'
        from tasks.models import Resolution
        fixed, _ = Resolution.objects.get_or_create(slug='fixed', defaults={'name': 'Fixed'})
        bug1.resolution = fixed
        bug1.save(update_fields=['status', 'resolution', 'updated_at'])

        qa2 = _make_qa_task(project, origin)
        _make_generation(project=project, origin=origin, qa_task=qa2, generation_key='g2')
        out2 = commit_disposition(
            task=qa2, idempotency_key='k2', disposition='fail',
            screen_data=_fail_payload('RECUR'), organization='shizuha',
        )
        bugs = list(Item.objects.filter(item_type='bug.qa_failure').order_by('created_at'))
        self.assertEqual(len(bugs), 2)
        self.assertNotEqual(bugs[0].pk, bugs[1].pk)  # new regression bug, not reopened
        self.assertEqual(bugs[0].status, 'done')     # predecessor untouched
        self.assertEqual(out2.result['product_bug']['id'], str(bugs[1].pk))
        # regression relation recorded
        reg = ItemLink.objects.filter(
            source_item=bugs[1], target_item=bugs[0], link_type='regresses',
        )
        self.assertEqual(reg.count(), 1)


class RollbackInjectionTests(TestCase):
    def test_rolled_back_fail_leaves_no_partial_effects(self):
        project = _make_project()
        origin = _make_origin(project=project)
        qa = _make_qa_task(project, origin)
        gen = _make_generation(project=project, origin=origin, qa_task=qa, generation_key='g1')

        def _boom(*a, **k):
            raise RuntimeError('injected failure')

        # Inject failure at the mechanical-factory comment-mint point (post-receipt,
        # pre-commit) — the whole transaction must roll back. The service imports
        # the factory lazily inside the transaction, so patch its definition site.
        with patch(
            'tasks.services.mechanical_comments.MechanicalCommentFactory.create',
            side_effect=_boom,
        ):
            with self.assertRaises(RuntimeError):
                commit_disposition(
                    task=qa, idempotency_key='k1', disposition='fail',
                    screen_data=_fail_payload('X'), organization='shizuha',
                )
        # No partial receipt / bug / state (re-read from DB — the in-memory
        # object was mutated before the injected failure; the transaction bound
        # it back).
        self.assertEqual(FocusedQADispositionReceipt.objects.count(), 0)
        self.assertEqual(Item.objects.filter(item_type='bug.qa_failure').count(), 0)
        self.assertEqual(Item.objects.get(pk=qa.pk).status, 'in_review')

        # The same request retries cleanly after rollback.
        out = commit_disposition(
            task=qa, idempotency_key='k1', disposition='fail',
            screen_data=_fail_payload('X'), organization='shizuha',
        )
        self.assertFalse(out.replayed)
        self.assertEqual(FocusedQADispositionReceipt.objects.count(), 1)
        self.assertEqual(Item.objects.filter(item_type='bug.qa_failure').count(), 1)
