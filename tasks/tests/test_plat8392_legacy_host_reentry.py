"""PLAT-8392: retired ``git.shizuha.com``-host PR rows must not wedge the
PLAT-1296/E review re-entry gate.

Live failure (PLAT-7725): the carrier PR was linked under the retired mirror
hostname ``https://git.shizuha.com/...`` (open, no head_sha). The supersession
WRITER annotated it ``superseded_by`` once the canonical Origin row was
provider-confirmed — but every re-entry READER (initial-submission scan,
no-bounce scan, bounce-key comparison) still treated the legacy row as a live
obligation, so the gate rejected every closure transition forever with
"no head_sha — re-run pulse_link_pr".

Contract locked here (fail-closed except where noted):
- a legacy-host row carrying a trusted ``superseded_by`` mapping to the
  SAME-identity canonical Origin URL that is LINKED with head provenance (and
  live-confirmed on re-entry paths) is retired — it carries no independent
  obligation in any of the three gate scans;
- no mapping / mapping to a different identity / canonical row without head
  provenance / canonical row unconfirmed on a re-entry path → the legacy row
  stays an open obligation and the gate fails closed;
- audit history is never rewritten: the row stays in ``metadata.pull_requests``.
"""

from __future__ import annotations

import re
from unittest import mock

from django.test import TestCase

from tasks.models import Comment
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
from tasks.tests.test_plat3159_review_reentry import (
    _item,
    _live_confirm,
    _validator_transition,
)

LEGACY_URL = 'https://git.shizuha.com/shizuha-labs/hive/pulls/277'
CANONICAL_URL = 'https://origin.shizuha.com/shizuha-labs/hive/pulls/277'
CANONICAL_HEAD = 'f0307bf443f9'


def _rows(*, with_mapping=True, canonical_head=CANONICAL_HEAD, mapped_identity=None):
    legacy_row = {
        'url': LEGACY_URL,
        'repo': 'shizuha-labs/hive',
        'number': 277,
        'status': 'open',
        # the live PLAT-7725 shape: persisted verbatim, never refreshed
        'head_sha': '',
    }
    if with_mapping:
        legacy_row['superseded_by'] = mapped_identity or CANONICAL_URL
    canonical_row = {
        'url': CANONICAL_URL,
        'repo': 'shizuha-labs/hive',
        'number': 277,
        'status': 'open',
        'head_sha': canonical_head,
    }
    return [legacy_row, canonical_row]


def _stamp_empty_bounce(item):
    """Bounce stamped while the ONLY linked row was the headless legacy row —
    the snapshot records no per-PR heads (the PLAT-7725 shape)."""
    meta = item.metadata
    meta['review_bounce_at'] = '2026-09-09T12:00:00+00:00'
    meta['review_bounce_pr_heads'] = {}
    item.metadata = meta
    item.save(update_fields=['metadata', 'updated_at'])
    item.refresh_from_db()


class TrustedRetiredRowHelperTests(TestCase):
    def test_retired_row_requires_mapping_linked_head_and_identity(self):
        prs = _rows()
        # full trust (no refresh gate) → retired
        self.assertEqual(
            WorkflowService._trusted_retired_pr_row_keys(prs), {LEGACY_URL},
        )
        # no mapping → not retired
        self.assertEqual(
            WorkflowService._trusted_retired_pr_row_keys(_rows(with_mapping=False)),
            set(),
        )
        # mapping to a DIFFERENT identity → hostile, fail closed
        self.assertEqual(
            WorkflowService._trusted_retired_pr_row_keys(
                _rows(mapped_identity='https://origin.shizuha.com/shizuha-labs/other/pulls/1'),
            ),
            set(),
        )
        # canonical row without head provenance → not retired
        self.assertEqual(
            WorkflowService._trusted_retired_pr_row_keys(_rows(canonical_head='')),
            set(),
        )
        # canonical row not linked on the task → not retired
        self.assertEqual(
            WorkflowService._trusted_retired_pr_row_keys([_rows()[0]]),
            set(),
        )

    def test_reentry_path_requires_live_confirmation_of_canonical(self):
        prs = _rows()
        confirmed = {CANONICAL_URL: True}
        self.assertEqual(
            WorkflowService._trusted_retired_pr_row_keys(prs, refresh_confirmed=confirmed),
            {LEGACY_URL},
        )
        self.assertEqual(
            WorkflowService._trusted_retired_pr_row_keys(prs, refresh_confirmed={}),
            set(),
        )


class NoBounceScanTests(TestCase):
    """Bounce stamped with an empty per-PR snapshot (headless legacy row was the
    only link at bounce time) → the gate takes the no-bounce scan."""

    def test_superseded_legacy_row_admits_with_scoped_rebuttal(self):
        item = _item(pull_requests=_rows())
        _stamp_empty_bounce(item)
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal (scope: shizuha-labs/hive#277): substance done, closure wedged only on the stale mirror link.',
        )
        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_unmapped_legacy_row_still_fails_closed(self):
        item = _item(pull_requests=_rows(with_mapping=False))
        _stamp_empty_bounce(item)
        # no rebuttal: the unmapped legacy row must keep the gate fail-closed
        with _live_confirm(item), self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(_validator_transition(), item)


class InitialSubmissionScanTests(TestCase):
    def test_superseded_legacy_row_does_not_block_first_submission(self):
        item = _item(pull_requests=_rows())
        ok, detail = WorkflowService._review_reentry_has_progress(item)
        self.assertTrue(ok, detail)

    def test_unmapped_legacy_row_blocks_first_submission(self):
        item = _item(pull_requests=_rows(with_mapping=False))
        ok, detail = WorkflowService._review_reentry_has_progress(item)
        self.assertFalse(ok)
        self.assertIn('hive#277', detail)


class BounceKeyComparisonTests(TestCase):
    def test_superseded_legacy_bounce_key_is_exempt(self):
        item = _item(pull_requests=_rows())
        meta = item.metadata
        meta['review_bounce_at'] = '2026-09-09T12:00:00+00:00'
        meta['review_bounce_pr_heads'] = {LEGACY_URL: ''}
        item.metadata = meta
        item.save(update_fields=['metadata', 'updated_at'])
        item.refresh_from_db()
        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_unmapped_legacy_row_is_auto_annotated_then_admitted(self):
        item = _item(pull_requests=_rows(with_mapping=False))
        meta = item.metadata
        meta['review_bounce_at'] = '2026-09-09T12:00:00+00:00'
        meta['review_bounce_pr_heads'] = {LEGACY_URL: ''}
        item.metadata = meta
        item.save(update_fields=['metadata', 'updated_at'])
        item.refresh_from_db()
        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise
        item.refresh_from_db()
        legacy = next(r for r in item.metadata['pull_requests']
                      if r.get('url') == LEGACY_URL)
        self.assertEqual(legacy.get('superseded_by'), CANONICAL_URL)

    def test_unconfirmable_canonical_row_keeps_gate_fail_closed(self):
        """Canonical row without head provenance → never confirmed → no
        annotation, legacy bounce key stays an unconfirmed obligation."""
        item = _item(pull_requests=_rows(canonical_head=''))
        meta = item.metadata
        meta['review_bounce_at'] = '2026-09-09T12:00:00+00:00'
        meta['review_bounce_pr_heads'] = {LEGACY_URL: ''}
        item.metadata = meta
        item.save(update_fields=['metadata', 'updated_at'])
        item.refresh_from_db()
        with _live_confirm(item), self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(_validator_transition(), item)

    def test_audit_row_preserved_after_admission(self):
        """Retirement never rewrites or deletes the legacy row."""
        item = _item(pull_requests=_rows())
        _stamp_empty_bounce(item)
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal (scope: shizuha-labs/hive#277): argued.',
        )
        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        item.refresh_from_db()
        rows = item.metadata['pull_requests']
        self.assertIn(LEGACY_URL, [r.get('url') for r in rows])
        legacy = next(r for r in rows if r.get('url') == LEGACY_URL)
        self.assertEqual(legacy.get('superseded_by'), CANONICAL_URL)
