"""PLAT-4675 (ORIG-18): review re-entry gate vs Origin PRs + retired GitHub mirror.

Post-cutover, github.com is a read-only push-mirror: a mirrored PR's head can
never advance, so evaluating it in the re-entry gate permanently fail-closes
every previously-bounced task.  With an active Origin PR linked, the gate must
evaluate provenance against the Origin PR and disregard frozen mirror links.
"""
import itertools
from types import SimpleNamespace

from django.test import TestCase

from tasks.models import Item
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
# PLS-496 / revi P1.2: the re-entry gate now requires a LIVE provider
# confirmation of each obligated PR head at admission; without a token it
# fail-closes.  Reuse the shared confirm-mock so advancement/unchanged tests
# exercise the intended path rather than the unconfirmed fail-closed path.
from tasks.tests.test_plat3159_review_reentry import _live_confirm

GH_URL = 'https://github.com/shizuha-labs/deploy/pull/503'
ORIGIN_URL = 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/10'
_key_counter = itertools.count(1)


def _item(pull_requests, metadata_extra=None):
    metadata = {'pull_requests': pull_requests}
    if metadata_extra:
        metadata.update(metadata_extra)
    return Item.objects.create(
        item_key=f'PLAT-4675-T{next(_key_counter)}',
        title='origin reentry test',
        status='in_progress',
        workflow_name='autonomous-dev',
        workflow_status='in_progress',
        assignee_id=101,
        assignee_email='author@agents.shizuha.io',
        created_by_id=101,
        created_by_email='author@agents.shizuha.io',
        metadata=metadata,
    )


def _validator_transition():
    return SimpleNamespace(
        validators=[{'type': 'review_reentry_requires_progress'}],
        from_status='in_progress',
        to_status='in_review',
    )


class Plat4675OriginReentryTests(TestCase):
    def test_frozen_mirror_not_waived_by_bare_active_origin_pr(self):
        # v8 rule 5 (revi #645 P1.2): a bare active Origin PR must NOT silently
        # waive a distinct frozen GitHub mirror. Without an EXACT per-row
        # superseded_by mapping (or exact scoped arbitration), the frozen bounced
        # mirror stays an open obligation and the gate blocks. (Was
        # test_frozen_mirror_bounce_ignored_when_origin_pr_active, which locked
        # the over-broad v6 behavior the v7 amendment rejects; the auto-recovery
        # positive with a superseded_by mapping is covered in test_plat3159.)
        item = _item(
            pull_requests=[
                {'url': GH_URL, 'repo': 'shizuha-labs/deploy', 'number': 503,
                 'status': 'open', 'head_sha': 'd4c02d6'},
                {'url': ORIGIN_URL, 'repo': 'shizuha-labs/deploy', 'number': 10,
                 'status': 'open', 'head_sha': 'origin-fix-head'},
            ],
            metadata_extra={
                'review_bounce_at': '2026-07-13T00:00:00Z',
                'review_bounce_pr_heads': {GH_URL: 'd4c02d6'},
            },
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(_validator_transition(), item)

    def test_pure_github_task_behavior_unchanged(self):
        # Legacy task with ONLY a GitHub PR, bounced, head unchanged -> still blocks.
        item = _item(
            pull_requests=[
                {'url': GH_URL, 'repo': 'shizuha-labs/deploy', 'number': 503,
                 'status': 'open', 'head_sha': 'd4c02d6'},
            ],
            metadata_extra={
                'review_bounce_at': '2026-07-13T00:00:00Z',
                'review_bounce_pr_heads': {GH_URL: 'd4c02d6'},
            },
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(_validator_transition(), item)

    def test_origin_pr_missing_head_sha_still_fails_closed(self):
        # Origin PR without provenance must still be rejected (fail-closed).
        item = _item(
            pull_requests=[
                {'url': ORIGIN_URL, 'repo': 'shizuha-labs/deploy', 'number': 10,
                 'status': 'open'},
            ],
        )
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(_validator_transition(), item)

    def test_origin_bounced_head_must_still_advance(self):
        # An Origin PR bounced and unchanged still blocks — the mirror carve-out
        # applies ONLY to github.com links.
        item = _item(
            pull_requests=[
                {'url': ORIGIN_URL, 'repo': 'shizuha-labs/deploy', 'number': 10,
                 'status': 'open', 'head_sha': 'same-head'},
            ],
            metadata_extra={
                'review_bounce_at': '2026-07-13T00:00:00Z',
                'review_bounce_pr_heads': {ORIGIN_URL: 'same-head'},
            },
        )
        with _live_confirm(item):
            with self.assertRaises(WorkflowValidationError):
                WorkflowService._run_validators(_validator_transition(), item)

    def test_origin_bounced_head_advanced_passes(self):
        item = _item(
            pull_requests=[
                {'url': ORIGIN_URL, 'repo': 'shizuha-labs/deploy', 'number': 10,
                 'status': 'open', 'head_sha': 'new-head'},
            ],
            metadata_extra={
                'review_bounce_at': '2026-07-13T00:00:00Z',
                'review_bounce_pr_heads': {ORIGIN_URL: 'old-head'},
            },
        )
        with _live_confirm(item):  # live head confirmed at 'new-head' → advanced
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise
