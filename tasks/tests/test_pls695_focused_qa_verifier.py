"""PLS-695 — Focused-QA verifier: typed evidence-schema selection + exhaustive discovery.

Regression matrix from the canonical contract (wiki 42e2042e v1 §6):
- Positive: complete no-PR Security record with no live-account fields; evidence
  strictly beyond page 1; evidence on a linked task; concurrent insertion during
  traversal with no skipped comment IDs.
- Negative: verdict/formal-review omitted; wrong SHA/page version; stale review
  ID; unknown/legacy artifact type fails closed to unclassified; exact-head
  mismatch despite a syntactically valid review reference.
- Adversarial: ambiguous artifact type; no-pr docs vs no-pr security; hybrid
  code+live; inaccessible evidence source.
"""

from __future__ import annotations

from django.test import TestCase

from tasks.models import Comment, Item, ItemLink, Project
from tasks.services.focused_qa_verifier import (
    ARTIFACT_CODE_PR,
    ARTIFACT_HYBRID,
    ARTIFACT_LIVE_QA,
    ARTIFACT_NO_PR_REVIEW,
    ARTIFACT_UNCLASSIFIED,
    OUTCOME_FAIL,
    OUTCOME_NOT_OBSERVABLE,
    OUTCOME_PASS,
    OUTCOME_UNCLASSIFIED,
    classify_artifact,
    read_all_comments,
    verify_item,
)
from tasks.tests.base import AuthenticatedAPITestCase

SHA_40 = 'cdf1a10cf3a5' + '0' * 28
SHA_40_WRONG = 'deadbeef' + '0' * 32
PAGE_ID = '42e2042e-23c2-4d45-8061-c2adaf194a3c'


def _make_item(project, **kwargs):
    defaults = dict(
        project=project,
        item_key=project.get_next_item_key(),
        title='Artifact',
        status='done',
        created_by_id=0,
        created_by_email='qa-automation@shizuha.com',
    )
    defaults.update(kwargs)
    return Item.objects.create(**defaults)


def _comment(item, content, author='reviewer@shizuha.com'):
    return Comment.objects.create(
        item=item, author_id=0, author_email=author, content=content,
    )


class ClassifyArtifactTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='P695', org_id=1, name='PLS-695')

    def test_no_pr_security_review(self):
        item = _make_item(self.project, title='Security review of auth flow',
                          labels={'source': 'no-pr'}, tags='no-pr,security')
        kind, inputs = classify_artifact(item)
        self.assertEqual(kind, ARTIFACT_NO_PR_REVIEW)
        self.assertIn('no_pr_signal', inputs)
        self.assertTrue(inputs['no_pr_signal'])

    def test_no_pr_docs_is_still_no_pr_review(self):
        # `no-pr` alone cannot discriminate docs vs security — both classify as
        # no_pr_review (the schema requires an explicit verdict/receipt, so a
        # docs-only artifact without one still fails closed).
        item = _make_item(self.project, title='Docs update', tags='no-pr,docs')
        kind, _ = classify_artifact(item)
        self.assertEqual(kind, ARTIFACT_NO_PR_REVIEW)

    def test_live_qa(self):
        item = _make_item(self.project, title='Live QA of deploy',
                          tags='live,qa,e2e')
        kind, _ = classify_artifact(item)
        self.assertEqual(kind, ARTIFACT_LIVE_QA)

    def test_code_pr(self):
        item = _make_item(self.project, title='Fix auth bug',
                          metadata={'pull_requests': [{'url': 'https://origin.shizuha.com/x/y/pulls/1'}]})
        kind, _ = classify_artifact(item)
        self.assertEqual(kind, ARTIFACT_CODE_PR)

    def test_hybrid_code_plus_live(self):
        item = _make_item(self.project, title='Deploy fix with live check',
                          tags='pr,deploy,qa', metadata={'pull_requests': [{'url': 'https://x/p/1'}]})
        kind, _ = classify_artifact(item)
        self.assertEqual(kind, ARTIFACT_HYBRID)

    def test_ambiguous_fails_closed_to_unclassified(self):
        # No discriminating signal → unclassified, never a silent default to live-QA.
        item = _make_item(self.project, title='Misc note', tags='')
        kind, _ = classify_artifact(item)
        self.assertEqual(kind, ARTIFACT_UNCLASSIFIED)


class SnapshotSafeTraversalTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='P695', org_id=1, name='PLS-695')
        self.item = _make_item(self.project, title='Traversal')

    def test_reads_all_comments_to_exhaustion(self):
        for i in range(5):
            _comment(self.item, f'comment {i}')
        comments, restarts = read_all_comments(self.item)
        self.assertEqual(len(comments), 5)
        self.assertEqual(restarts, 0)
        # Ascending id order.
        ids = [c['id'] for c in comments]
        self.assertEqual(ids, sorted(ids))

    def test_no_skipped_ids_under_concurrent_insert(self):
        # Simulate a concurrent insert: after the first page is read, a new
        # comment with an id inside the already-read window appears. The
        # traversal must detect the shifted window, restart, and read every id
        # — no skipped rows, no duplicates.
        for i in range(3):
            _comment(self.item, f'early {i}')
        from unittest.mock import patch

        real_rows = list(
            Comment.objects.filter(item_id=self.item.pk).order_by('id')
            .values('id', 'author_email', 'content', 'created_at')
        )
        state = {'calls': 0}

        class FlakyQuerySet:
            def __init__(self, high_water):
                self._high_water = high_water

            def filter(self, *a, **k):
                return self

            def order_by(self, *a, **k):
                return self

            def values(self, *fields):
                return self

            def __getitem__(self, sl):
                state['calls'] += 1
                rows = [r for r in real_rows if r['id'] > self._high_water]
                if state['calls'] == 2 and rows:
                    # Concurrent insert shifted the window: return a row whose
                    # id is inside the already-read window.
                    new_row = dict(rows[0])
                    new_row['id'] = rows[0]['id'] - 1
                    new_row['content'] = 'concurrent insert'
                    return [new_row, rows[0]]
                return rows[sl]

        def fake_filter(**kwargs):
            return FlakyQuerySet(kwargs.get('id__gt', 0))

        with patch.object(Comment.objects, 'filter', side_effect=fake_filter):
            comments, restarts = read_all_comments(self.item, page_size=2)
        self.assertGreaterEqual(restarts, 1, 'shifted window must trigger a restart')
        ids = [c['id'] for c in comments]
        self.assertEqual(len(ids), len(set(ids)), 'no duplicate comment ids')
        real_ids = {r['id'] for r in real_rows}
        self.assertTrue(real_ids.issubset(set(ids)), 'no real comment id skipped')


class VerifierPositiveTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='P695', org_id=1, name='PLS-695')

    def test_no_pr_security_record_with_no_live_account_fields_passes(self):
        item = _make_item(self.project, title='Security review of auth flow',
                          labels={'source': 'no-pr'}, tags='no-pr,security')
        _comment(item, (
            f'## Security review\n'
            f'Target: shizuha-labs/deploy#{91} @ `{SHA_40}` (page `{PAGE_ID}` v1)\n'
            f'Dispositions: auth=passed, tenancy=passed, quota=passed\n'
            f'Reviewer: akira@shizuha.com\n'
            f'Verdict: APPROVED\n'
            f'Formal review #481; check run 1445 green; 14/14 regressions passed.'
        ))
        verdict = verify_item(item)
        self.assertEqual(verdict.outcome, OUTCOME_PASS)
        self.assertEqual(verdict.artifact_kind, ARTIFACT_NO_PR_REVIEW)
        # No live-account fields required for a no-PR review.
        self.assertNotIn('runtime_account_or_env', [f.field for f in verdict.findings])

    def test_evidence_beyond_page_one_passes(self):
        # Evidence strictly beyond page 1 (page_size=200) with routing churn.
        item = _make_item(self.project, title='Security review', tags='no-pr,security')
        for i in range(210):
            _comment(item, f'system routing comment {i}', author='system@pulse')
        _comment(item, (
            f'Target: shizuha-labs/deploy#{91} @ `{SHA_40}`\n'
            f'Dispositions: all passed\nReviewer: akira@shizuha.com\n'
            f'Verdict: APPROVED\nFormal review #481; 14/14 regressions passed.'
        ))
        verdict = verify_item(item)
        self.assertEqual(verdict.outcome, OUTCOME_PASS)
        comments_source = next(s for s in verdict.sources if s.kind == 'comments')
        self.assertGreaterEqual(comments_source.pages_traversed, 2)
        self.assertEqual(len(comments_source.comment_ids), 211)

    def test_evidence_on_linked_task_passes(self):
        subject = _make_item(self.project, title='Security review', tags='no-pr,security')
        companion = _make_item(self.project, title='Companion evidence')
        ItemLink.objects.create(
            source_item=subject, target_item=companion, link_type='relates_to',
            created_by_id=0, created_by_email='qa-automation@shizuha.com',
        )
        _comment(companion, (
            f'Target: shizuha-labs/deploy#{91} @ `{SHA_40}`\n'
            f'Dispositions: all passed\nReviewer: akira@shizuha.com\n'
            f'Verdict: APPROVED\nFormal review #481; 14/14 regressions passed.'
        ))
        verdict = verify_item(subject)
        self.assertEqual(verdict.outcome, OUTCOME_PASS)
        linked = [s for s in verdict.sources if s.kind == 'linked_task']
        self.assertTrue(linked, 'linked task followed as a source')


class VerifierNegativeTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='P695', org_id=1, name='PLS-695')

    def _no_pr_item(self):
        return _make_item(self.project, title='Security review', tags='no-pr,security')

    def test_verdict_omitted_fails(self):
        item = self._no_pr_item()
        _comment(item, (
            f'Target: shizuha-labs/deploy#{91} @ `{SHA_40}`\n'
            f'Dispositions: all passed\nReviewer: akira@shizuha.com\n'
            f'Formal review #481; 14/14 regressions passed.'
        ))
        verdict = verify_item(item)
        self.assertEqual(verdict.outcome, OUTCOME_FAIL)

    def test_formal_review_omitted_fails(self):
        item = self._no_pr_item()
        _comment(item, (
            f'Target: shizuha-labs/deploy#{91} @ `{SHA_40}`\n'
            f'Dispositions: all passed\nReviewer: akira@shizuha.com\n'
            f'Verdict: APPROVED'
        ))
        verdict = verify_item(item)
        self.assertEqual(verdict.outcome, OUTCOME_FAIL)

    def test_wrong_sha_fails(self):
        # Task declares target head SHA_40; evidence binds to a DIFFERENT SHA.
        item = _make_item(
            self.project, title='Security review', tags='no-pr,security',
            metadata={'commit_sha': SHA_40},
        )
        _comment(item, (
            f'Target: shizuha-labs/deploy#{91} @ `{SHA_40_WRONG}`\n'
            f'Dispositions: all passed\nReviewer: akira@shizuha.com\n'
            f'Verdict: APPROVED\nFormal review #481; 14/14 regressions passed.'
        ))
        verdict = verify_item(item)
        self.assertEqual(verdict.outcome, OUTCOME_FAIL)

    def test_stale_review_id_fails(self):
        # A syntactically valid review reference that does not resolve to the
        # stated target (stale/copied) must fail.
        item = _make_item(
            self.project, title='Security review', tags='no-pr,security',
            metadata={'formal_review_id': '481'},
        )
        _comment(item, (
            f'Target: shizuha-labs/deploy#{91} @ `{SHA_40}`\n'
            f'Dispositions: all passed\nReviewer: akira@shizuha.com\n'
            f'Verdict: APPROVED\nFormal review #999 (stale); 0/14 regressions passed.'
        ))
        verdict = verify_item(item)
        self.assertEqual(verdict.outcome, OUTCOME_FAIL)

    def test_unknown_artifact_type_fails_closed_to_unclassified(self):
        item = _make_item(self.project, title='Misc', tags='')
        verdict = verify_item(item)
        self.assertEqual(verdict.outcome, OUTCOME_UNCLASSIFIED)
        self.assertEqual(verdict.artifact_kind, ARTIFACT_UNCLASSIFIED)
        self.assertNotIn('evidence absent', verdict.reason)

    def test_empty_no_pr_review_fails(self):
        item = self._no_pr_item()
        verdict = verify_item(item)
        self.assertEqual(verdict.outcome, OUTCOME_FAIL)


class VerifierAdversarialTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='P695', org_id=1, name='PLS-695')

    def test_hybrid_requires_live_runtime_fields(self):
        item = _make_item(self.project, title='Deploy fix with live check',
                          tags='pr,deploy,qa',
                          metadata={'pull_requests': [{'url': 'https://x/p/1'}]})
        _comment(item, (
            f'PR #1 merged; CI green; commit `{SHA_40}`\n'
            f'Live check: account=qa@shizuha.com env=prod\n'
            f'URL https://app.shizuha.com/login exercised\n'
            f'Expected login works; observed login works\n'
            f'2026-08-22 14:00 env=prod\n'
            f'build sha256:{SHA_40}'
        ))
        verdict = verify_item(item)
        self.assertEqual(verdict.artifact_kind, ARTIFACT_HYBRID)
        self.assertEqual(verdict.outcome, OUTCOME_PASS)

    def test_hybrid_without_live_fields_fails(self):
        item = _make_item(self.project, title='Deploy fix with live check',
                          tags='pr,deploy,qa',
                          metadata={'pull_requests': [{'url': 'https://x/p/1'}]})
        _comment(item, (
            f'PR #1 merged; CI green; commit `{SHA_40}`\n'
            f'Verdict: APPROVED\nFormal review #481.'
        ))
        verdict = verify_item(item)
        self.assertEqual(verdict.artifact_kind, ARTIFACT_HYBRID)
        self.assertEqual(verdict.outcome, OUTCOME_FAIL)

    def test_inaccessible_linked_source_is_not_observable_not_absent(self):
        subject = _make_item(self.project, title='Security review', tags='no-pr,security')
        companion = _make_item(self.project, title='Companion evidence')
        ItemLink.objects.create(
            source_item=subject, target_item=companion, link_type='relates_to',
            created_by_id=0, created_by_email='qa-automation@shizuha.com',
        )
        # The linked source exists but is unreadable (e.g. permission-denied /
        # upstream error). It must be recorded as unreadable — never rendered
        # as searched-and-absent.
        from unittest.mock import patch
        with patch(
            'tasks.services.focused_qa_verifier._resolve_item',
            side_effect=Exception('upstream review API error'),
        ):
            verdict = verify_item(subject)
        unreadable = [s for s in verdict.sources if s.terminal_state == 'unreadable']
        self.assertTrue(unreadable)
        self.assertNotIn('searched_and_absent', verdict.reason)

    def test_verdict_never_mutates_source(self):
        item = _make_item(self.project, title='Security review', tags='no-pr,security')
        before = Comment.objects.filter(item_id=item.pk).count()
        verify_item(item)
        after = Comment.objects.filter(item_id=item.pk).count()
        self.assertEqual(before, after, 'verifier must never write to the source')


class VerifierApiTests(AuthenticatedAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(key='P695', org_id=1, name='PLS-695')

    def test_focused_qa_verify_endpoint_returns_verdict(self):
        item = _make_item(self.project, title='Security review', tags='no-pr,security')
        _comment(item, (
            f'Target: shizuha-labs/deploy#{91} @ `{SHA_40}`\n'
            f'Dispositions: all passed\nReviewer: akira@shizuha.com\n'
            f'Verdict: APPROVED\nFormal review #481; 14/14 regressions passed.'
        ))
        resp = self.client.get(f'/api/items/{item.pk}/focused-qa-verify/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['outcome'], OUTCOME_PASS)
        self.assertEqual(data['artifact_kind'], ARTIFACT_NO_PR_REVIEW)
        self.assertEqual(data['evidence_schema_version'], 'focused-qa.no-pr-review.v1')
        self.assertIn('classifier_inputs', data)
        self.assertIn('sources', data)
        self.assertIn('findings', data)

    def test_focused_qa_verify_endpoint_does_not_mutate(self):
        item = _make_item(self.project, title='Security review', tags='no-pr,security')
        before = Comment.objects.filter(item_id=item.pk).count()
        self.client.get(f'/api/items/{item.pk}/focused-qa-verify/')
        after = Comment.objects.filter(item_id=item.pk).count()
        self.assertEqual(before, after)
