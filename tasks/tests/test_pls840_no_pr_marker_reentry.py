"""PLS-840: a literal ``no-pr`` marker declares no obligation, it is not one.

Regression for the PLAT-5519 wedge: a read-only attestation task whose scope
forbids producing code carries ``no-pr`` as its only linked row.  Before this
change the PLAT-1296/E re-entry gate evaluated that marker as an OPEN obligation
lacking provenance — and because the marker names no provider artifact it can
never acquire a ``head_sha``, never be provider-confirmed terminal, and never be
superseded.  Re-entry was therefore refused permanently, with an error advising
two remedies ("push the fix(es)" / "post a PLAT-202 rebuttal") that are both
unreachable for that row class.

The pair of tests below is the point.  Admitting the marker-only task proves the
wedge is gone; refusing the marker-PLUS-real-PR task proves the marker did not
become a bypass.  Either alone is consistent with the gate simply having been
weakened, which is the defect class this repo keeps re-learning.
"""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from tasks.services.workflow_service import WorkflowService


def _item(prs, *, bounced=None, key='PLAT-5519'):
    meta = {'pull_requests': list(prs)}
    if bounced is not None:
        meta['review_bounce_at'] = '2026-08-02T19:50:00+05:30'
        meta['review_bounce_pr_heads'] = dict(bounced)
    return SimpleNamespace(item_key=key, pk=23783, metadata=meta)


NO_PR = {'url': 'no-pr', 'status': 'open'}
REAL_PR = {
    'url': 'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/900',
    'repo': 'shizuha-labs/shizuha-tasks', 'number': 900,
    'status': 'open', 'head_sha': 'aaaaaaaaaaaa',
}


class NoPrMarkerIsNotAnObligationTests(SimpleTestCase):
    def test_marker_is_recognised_only_on_an_exact_bare_value(self):
        self.assertTrue(WorkflowService._is_no_pr_marker(NO_PR))
        self.assertTrue(WorkflowService._is_no_pr_marker({'url': '  NO-PR  '}))

        # Hostile / lookalike values must NOT waive an obligation.  This
        # predicate is the one place a row escapes the gate, so anything that
        # merely resembles the marker stays fail-closed.
        for hostile in (
            {'url': 'https://origin.shizuha.com/shizuha-labs/no-pr/pulls/1',
             'repo': 'shizuha-labs/no-pr', 'number': 1},
            {'url': 'no-pr', 'repo': 'shizuha-labs/x', 'number': 7},
            {'url': 'no-pr-really'},
            {'url': 'https://example.test/no-pr'},
            {'url': ''},
            {},
        ):
            with self.subTest(hostile=hostile):
                self.assertFalse(WorkflowService._is_no_pr_marker(hostile))

    def test_marker_only_bounced_task_can_re_enter(self):
        """The PLAT-5519 shape: bounced, marker is the ONLY linked row."""
        item = _item([NO_PR], bounced={})
        allowed, detail = WorkflowService._review_reentry_has_progress(item, {})
        self.assertTrue(allowed)
        self.assertIsNone(detail)

    def test_marker_does_not_waive_a_real_unchanged_obligation(self):
        """The load-bearing negative: marker alongside a real open PR.

        Without this, the fix above is indistinguishable from deleting the gate.
        """
        item = _item([NO_PR, REAL_PR], bounced={REAL_PR['url']: REAL_PR['head_sha']})
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata',
            return_value=(False, {REAL_PR['url']: True}),
        ), mock.patch.object(
            WorkflowService, '_has_review_rebuttal_since_bounce', return_value=False,
        ):
            allowed, detail = WorkflowService._review_reentry_has_progress(item, {})
        self.assertFalse(allowed)
        self.assertIn('900', str(detail))

    def test_refusal_no_longer_advises_impossible_remedies_for_the_marker(self):
        """The marker must not appear in a rejection naming unsatisfiable rows.

        It was previously reported as ``no-pr (no head_sha — re-run
        pulse_link_pr)`` — advice that can never be followed for a row with no
        provider artifact.

        The needle is ``no-pr (`` rather than a bare ``no-pr`` (banto) or the
        single literal ``no-pr (no head_sha`` (banto's proposed patch):

        * bare ``no-pr`` also matches the LEGITIMATE advice string
          ``add the 'no-pr' label to declare that explicitly`` raised by
          ``_run_validators``, so it can fail for the opposite of the reason it
          names — a wrongly-RED fixture asserting a leak where the code in fact
          gave correct guidance;
        * ``no-pr (no head_sha`` is blind to the marker's OTHER leak form,
          ``no-pr (no bounce record)``, emitted a few lines below the one the
          docstring records — same defect, different parenthetical.

        A label only ever reaches remediation text as ``<label> (<reason>)``,
        and the legitimate advice always quotes the flag, so ``no-pr (`` covers
        every leak shape while matching no correct output.
        """
        item = _item([NO_PR, REAL_PR], bounced={REAL_PR['url']: REAL_PR['head_sha']})
        with mock.patch.object(
            WorkflowService, '_refresh_review_reentry_pr_metadata',
            return_value=(False, {REAL_PR['url']: True}),
        ), mock.patch.object(
            WorkflowService, '_has_review_rebuttal_since_bounce', return_value=False,
        ):
            _allowed, detail = WorkflowService._review_reentry_has_progress(item, {})
        self.assertNotIn('no-pr (', str(detail))

    def test_obligation_set_excludes_the_marker_but_linked_rows_still_see_it(self):
        """Scope guard: the marker is dropped from OBLIGATIONS only.

        The submit-for-review gate requires "a linked PR or the no-pr label";
        if this fix had filtered the marker out of the shared linked-row helper,
        that gate would stop seeing it and a no-PR task would break elsewhere.
        """
        item = _item([NO_PR])
        self.assertEqual(WorkflowService._reentry_obligation_prs(item), [])
        self.assertEqual(len(WorkflowService._active_linked_pr_entries(item)), 1)
        self.assertEqual(len(WorkflowService._linked_pr_entries(item)), 1)


GITHUB_MIRROR_ROW = {
    'url': 'https://github.com/shizuha-labs/shizuha-tasks/pull/500',
    'repo': 'shizuha-labs/shizuha-tasks', 'number': 500,
    'status': 'open', 'head_sha': 'mirror-head',
}


class SentinelMustNotCollapseAMirrorObligationTests(SimpleTestCase):
    """rei P1#2: sentinel + open GitHub row must not yield ZERO obligations.

    Ordering bug: the sentinel is not a github.com URL, so it counted as a
    non-mirror row and suppressed the mirror fallback in
    ``_active_linked_pr_entries``. Filtering the sentinel AFTERWARDS then left an
    empty list — and v21 rule 5's "a frozen GitHub row that still reports open
    remains an open obligation" silently disappeared.

    The sentinel must be stripped BEFORE selection, so the mirror fallback still
    sees the only real row.
    """

    def test_sentinel_plus_open_mirror_keeps_the_mirror_obligation(self):
        item = _item([NO_PR, GITHUB_MIRROR_ROW])
        obligations = WorkflowService._reentry_obligation_prs(item)
        self.assertNotEqual(
            obligations, [],
            msg='sentinel + open mirror collapsed to zero obligations',
        )
        self.assertEqual([p['url'] for p in obligations], [GITHUB_MIRROR_ROW['url']])

    def test_sentinel_plus_origin_row_still_prefers_origin(self):
        """The PLAT-4675 behaviour this must not disturb."""
        item = _item([NO_PR, GITHUB_MIRROR_ROW, REAL_PR])
        obligations = WorkflowService._reentry_obligation_prs(item)
        self.assertEqual([p['url'] for p in obligations], [REAL_PR['url']])

    def test_bounce_stamping_keys_the_mirror_not_the_sentinel(self):
        """The same ordering bug reached bounce stamping."""
        item = _item([NO_PR, GITHUB_MIRROR_ROW])
        prs = WorkflowService._reentry_obligation_prs(item)
        keys = {(p.get('url') or '').rstrip('/') for p in prs}
        self.assertIn(GITHUB_MIRROR_ROW['url'], keys)
        self.assertNotIn('no-pr', keys)
