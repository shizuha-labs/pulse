"""PLAT-6226: review re-entry gate must not wedge on frozen Origin dev repos.

A frozen Origin dev repo (e.g. ``shizuha-labs/shizuha``, archived 2026-08-15,
split-brain stop) returns HTTP 404 from the provider, so its linked PR's head
can never be live-confirmed and the re-entry gate would fail-close the task
forever — even when the real fix rides a fresh, live-confirmed active Origin
PR (e.g. ``shizuha-beta#216``). This mirrors the retired-GitHub-mirror carve-out
(PLAT-4675 Facet 2 / ORIG-18) for frozen Origin dev lines, with the same
security preconditions: the exempting Origin PR must be exact, live-confirmed,
currently open, carry provenance, and NOT itself be on a frozen repo.
"""
import itertools
import os
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from tasks.models import Item
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError

FROZEN_URL = 'https://origin.shizuha.com/shizuha-labs/shizuha/pulls/27'
FIX_URL = 'https://origin.shizuha.com/shizuha-labs/shizuha-beta/pulls/216'
_key_counter = itertools.count(1)


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload

    def json(self):
        return self._payload


def _item(pull_requests, metadata_extra=None):
    metadata = {'pull_requests': pull_requests}
    if metadata_extra:
        metadata.update(metadata_extra)
    return Item.objects.create(
        item_key=f'PLAT-6226-T{next(_key_counter)}',
        title='frozen origin reentry test',
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


def _confirm_frozen_fails(confirm_fix=True):
    """Mock provider refresh: frozen-repo PR 404s; fix PR confirms open.

    Returns a context manager patching ``tasks.tasks._linked_pr_api_request``.
    """
    import tasks.tasks as tasks_mod
    from unittest import mock

    def live_request(url, **_kwargs):
        key = str(url or '').rstrip('/')
        if key == FROZEN_URL:
            raise RuntimeError('frozen repo 404')
        if key == FIX_URL and confirm_fix:
            return (
                'forgejo', 'shizuha-labs', 'shizuha-beta', 216,
                _FakeResponse(200, {'state': 'open', 'head': {'sha': '6b497903'}}),
            )
        raise RuntimeError('unknown url')

    return mock.patch.object(tasks_mod, '_linked_pr_api_request', side_effect=live_request)


@mock.patch.dict(os.environ, {'FORGEJO_TOKEN': 'test-token', 'GITHUB_TOKEN': ''})
class Plat6226FrozenOriginReentryTests(TestCase):
    def test_frozen_origin_pr_exempt_when_active_fix_pr_confirmed(self):
        """SCLI-582 shape: frozen shizuha#27 bounced + live-confirmed shizuha-beta#216 → pass."""
        item = _item(
            pull_requests=[
                {'url': FROZEN_URL, 'repo': 'shizuha-labs/shizuha', 'number': 27,
                 'status': 'open', 'head_sha': 'd936674d'},
                {'url': FIX_URL, 'repo': 'shizuha-labs/shizuha-beta', 'number': 216,
                 'status': 'open', 'head_sha': '6b497903'},
            ],
            metadata_extra={
                'review_bounce_at': '2026-08-15T00:00:00Z',
                'review_bounce_pr_heads': {FROZEN_URL: 'd936674d'},
            },
        )
        with _confirm_frozen_fails():
            # Must NOT raise — the frozen Origin repo PR is exempt because a
            # fresh live-confirmed active Origin PR holds the fix.
            WorkflowService._run_validators(_validator_transition(), item)

    def test_frozen_origin_pr_alone_still_fails_closed(self):
        """No active fix PR → the frozen row stays an open obligation."""
        item = _item(
            pull_requests=[
                {'url': FROZEN_URL, 'repo': 'shizuha-labs/shizuha', 'number': 27,
                 'status': 'open', 'head_sha': 'd936674d'},
            ],
            metadata_extra={
                'review_bounce_at': '2026-08-15T00:00:00Z',
                'review_bounce_pr_heads': {FROZEN_URL: 'd936674d'},
            },
        )
        with _confirm_frozen_fails():
            with self.assertRaises(WorkflowValidationError):
                WorkflowService._run_validators(_validator_transition(), item)

    def test_frozen_origin_pr_not_exempt_when_fix_pr_unconfirmed(self):
        """Fix PR not live-confirmed → the frozen row is not waived (fail-closed)."""
        item = _item(
            pull_requests=[
                {'url': FROZEN_URL, 'repo': 'shizuha-labs/shizuha', 'number': 27,
                 'status': 'open', 'head_sha': 'd936674d'},
                {'url': FIX_URL, 'repo': 'shizuha-labs/shizuha-beta', 'number': 216,
                 'status': 'open', 'head_sha': '6b497903'},
            ],
            metadata_extra={
                'review_bounce_at': '2026-08-15T00:00:00Z',
                'review_bounce_pr_heads': {FROZEN_URL: 'd936674d'},
            },
        )
        with _confirm_frozen_fails(confirm_fix=False):
            with self.assertRaises(WorkflowValidationError):
                WorkflowService._run_validators(_validator_transition(), item)

    def test_non_frozen_origin_pr_is_not_exempt(self):
        """A normal (non-frozen) Origin repo PR unchanged still blocks."""
        normal_url = 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/10'
        item = _item(
            pull_requests=[
                {'url': normal_url, 'repo': 'shizuha-labs/deploy', 'number': 10,
                 'status': 'open', 'head_sha': 'same-head'},
                {'url': FIX_URL, 'repo': 'shizuha-labs/shizuha-beta', 'number': 216,
                 'status': 'open', 'head_sha': '6b497903'},
            ],
            metadata_extra={
                'review_bounce_at': '2026-08-15T00:00:00Z',
                'review_bounce_pr_heads': {normal_url: 'same-head'},
            },
        )

        def live_request(url, **_kwargs):
            key = str(url or '').rstrip('/')
            if key == normal_url:
                return (
                    'forgejo', 'shizuha-labs', 'deploy', 10,
                    _FakeResponse(200, {'state': 'open', 'head': {'sha': 'same-head'}}),
                )
            if key == FIX_URL:
                return (
                    'forgejo', 'shizuha-labs', 'shizuha-beta', 216,
                    _FakeResponse(200, {'state': 'open', 'head': {'sha': '6b497903'}}),
                )
            raise RuntimeError('unknown url')

        import tasks.tasks as tasks_mod
        from unittest import mock

        with mock.patch.object(tasks_mod, '_linked_pr_api_request', side_effect=live_request):
            with self.assertRaises(WorkflowValidationError):
                WorkflowService._run_validators(_validator_transition(), item)

    def test_frozen_origin_pr_exempt_when_no_pr_declared(self):
        """PLAT-6952: a no-pr task (fix landed via repo restructure) with only a
        stale frozen-repo PR link must not wedge — the no-pr declaration waives
        the frozen key without requiring an active Origin PR."""
        item = _item(
            pull_requests=[
                {'url': FROZEN_URL, 'repo': 'shizuha-labs/shizuha', 'number': 27,
                 'status': 'open', 'head_sha': 'd936674d'},
            ],
            metadata_extra={
                'review_bounce_at': '2026-08-15T00:00:00Z',
                'review_bounce_pr_heads': {FROZEN_URL: 'd936674d'},
            },
        )
        item.labels = ['no-pr']
        item.save(update_fields=['labels'])
        with _confirm_frozen_fails():
            # Must NOT raise — the frozen Origin repo PR is exempt because the
            # task declares no-pr (no code-PR obligation exists).
            WorkflowService._run_validators(_validator_transition(), item)

    def test_frozen_origin_pr_exempt_when_no_pr_marker_row(self):
        """PLAT-6952: the exact ``no-pr`` marker row also declares no-pr and
        waives a stale frozen-repo PR key."""
        item = _item(
            pull_requests=[
                {'url': 'no-pr'},
                {'url': FROZEN_URL, 'repo': 'shizuha-labs/shizuha', 'number': 27,
                 'status': 'open', 'head_sha': 'd936674d'},
            ],
            metadata_extra={
                'review_bounce_at': '2026-08-15T00:00:00Z',
                'review_bounce_pr_heads': {FROZEN_URL: 'd936674d'},
            },
        )
        with _confirm_frozen_fails():
            WorkflowService._run_validators(_validator_transition(), item)

    def test_non_frozen_pr_not_exempt_even_with_no_pr(self):
        """PLAT-6952 security: the no-pr exemption covers ONLY frozen-repo keys.
        A no-pr task that also carries an unchanged PR on a LIVE repo still fails
        closed on that obligation."""
        normal_url = 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/10'
        item = _item(
            pull_requests=[
                {'url': normal_url, 'repo': 'shizuha-labs/deploy', 'number': 10,
                 'status': 'open', 'head_sha': 'same-head'},
            ],
            metadata_extra={
                'review_bounce_at': '2026-08-15T00:00:00Z',
                'review_bounce_pr_heads': {normal_url: 'same-head'},
            },
        )
        item.labels = ['no-pr']
        item.save(update_fields=['labels'])

        def live_request(url, **_kwargs):
            key = str(url or '').rstrip('/')
            if key == normal_url:
                return (
                    'forgejo', 'shizuha-labs', 'deploy', 10,
                    _FakeResponse(200, {'state': 'open', 'head': {'sha': 'same-head'}}),
                )
            raise RuntimeError('unknown url')

        import tasks.tasks as tasks_mod
        from unittest import mock

        with mock.patch.object(tasks_mod, '_linked_pr_api_request', side_effect=live_request):
            with self.assertRaises(WorkflowValidationError):
                WorkflowService._run_validators(_validator_transition(), item)
