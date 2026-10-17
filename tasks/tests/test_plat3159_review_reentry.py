"""PLAT-3159 / PLS-441: review re-entry requires PR progress or PLAT-202 rebuttal.

PLS-441 contract (per-PR, fail-closed provenance):
- Zero linked PR rows => exempt (no-PR task).
- First submission with current linked-PR heads but no bounce marker => exempt.
- Linked rows with missing current/bounce head, removed bounce PR, or a
  current head not freshly confirmed since the bounce => reject with a
  sync-required error naming each PR.
- ALL bounced PRs must advance.  One advanced PR never excuses another
  unchanged one; a newly-added PR never erases an unchanged bounced PR.
- Provider-confirmed terminal closed/merged PRs are audit-only automatically.
  Historical v6 active-tuple designations are inert under the v7 contract:
  stale or no-longer-authoritative comments cannot poison admission, and they
  cannot waive a separate unchanged open obligation.
- An author PLAT-202 rebuttal may bypass only the unchanged PRs it covers.
  A generic marker bypasses all unchanged PRs (single-PR tasks).  On multi-PR
  tasks, a scoped rebuttal must name each unchanged ``owner/repo#N`` or use
  ``all linked PRs``.
"""
import contextlib
import itertools
import json
import re
from types import SimpleNamespace
from unittest import mock

import requests

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from tasks.models import Comment, Item, ItemActivity, TeamRoster
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
from tasks.views import GitHubWebhookView


PR_URL = 'https://github.com/shizuha-labs/shizuha-tasks/pull/3159'
PR2_URL = 'https://github.com/shizuha-labs/agent/pull/10'
# PLS-496: Origin/Forgejo PRs key on ``origin.shizuha.com`` with a ``/pulls/N``
# path (note the plural, vs GitHub's ``/pull/N``).  Two Origin PRs are needed to
# exercise scoped recognition: a lone unchanged PR short-circuits (a generic
# rebuttal covers a single-PR task), and a github.com PR alongside an Origin PR
# is skipped as a retired mirror (PLAT-4675 Facet 2) — so only two non-mirror
# Origin PRs force the ``owner/repo#N`` scoping path the fix restores.
ORIGIN_PR_URL = 'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/22'
ORIGIN_PR2_URL = 'https://origin.shizuha.com/shizuha-labs/agent/pulls/7'


def _import_pulse_server():
    """Import the MCP server module beside the Django project (repo_root/
    mcp-server), the same sys.path bridge test_mcp_schema_contract uses, so a
    cross-layer test can drive the real pulse_server.link_pr path."""
    import os
    import sys
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    mcp_dir = os.path.join(repo_root, "mcp-server")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    os.environ.setdefault("SHIZUHA_PULSE_URL", "http://localhost:8002/pulse/api")
    import pulse_server
    return pulse_server
_key_counter = itertools.count(1)


def _item(*, head_sha='same-head', pull_requests=None, labels=None):
    if pull_requests is None:
        pull_requests = [{
            'url': PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 3159,
            'status': 'open',
            'head_sha': head_sha,
        }]
    return Item.objects.create(
        item_key=f'PLAT-3159-T{next(_key_counter)}',
        title='review reentry test',
        status='in_progress',
        workflow_name='autonomous-dev',
        workflow_status='in_progress',
        assignee_id=101,
        assignee_email='author@agents.shizuha.io',
        created_by_id=101,
        created_by_email='author@agents.shizuha.io',
        metadata={'pull_requests': pull_requests},
        labels=labels or [],
    )


def _validator_transition():
    return SimpleNamespace(
        validators=[{'type': 'review_reentry_requires_progress'}],
        from_status='in_progress',
        to_status='in_review',
    )


def _stamp_bounce(item):
    transition = SimpleNamespace(id='request-changes', post_functions=[{'type': 'stamp_review_bounce'}])
    WorkflowService.execute_post_functions(transition, item)
    item.save(update_fields=['metadata', 'updated_at'])
    item.refresh_from_db()


def _architect_active_tuple_comment(item, *, active_url=PR2_URL, active_head='def', superseded=None, author='aoi@shizuha.com'):
    if superseded is None:
        superseded = [{'url': PR_URL, 'head': 'abc'}]
    payload = {'active': {'url': active_url, 'head': active_head}, 'superseded': superseded}
    return Comment.objects.create(
        item=item,
        author_id=202,
        author_email=author,
        content='review-reentry-active-tuple-v1: ' + json.dumps(payload, sort_keys=True),
    )


@contextlib.contextmanager
def _live_confirm(item, overrides=None):
    """PLS-496 (revi P1.2 / v3 rule 8): admit only PRs whose LIVE provider head
    is confirmed.  Patch env + ``tasks.tasks._linked_pr_api_request`` so EACH
    linked PR in ``item.metadata['pull_requests']`` is confirmed at its current
    stored ``head_sha`` (or ``overrides[url]`` to simulate a live head that
    differs from the stored one).  The PR list is re-read lazily inside
    ``live_request`` so it reflects the item state at call time."""
    overrides = overrides or {}

    def live_request(url, **_kwargs):
        prs = (getattr(item, 'metadata', None) or {}).get('pull_requests') or []
        key = str(url or '').rstrip('/')
        pr = next(
            (p for p in prs if isinstance(p, dict)
             and str(p.get('url') or '').rstrip('/') == key),
            {},
        )
        head = overrides.get(url)
        if head is None:
            head = overrides.get(key)
        if head is None:
            head = pr.get('head_sha')
        m = re.search(r'/([^/]+)/([^/]+)/pulls?/(\d+)', url)
        owner, repo, number = m.groups() if m else ('owner', 'repo', '0')
        provider = 'github' if 'github.com' in url else 'forgejo'
        # Build a provider payload that round-trips through
        # ``_linked_pr_status_from_payload`` back to the PR's stored status, so a
        # live confirm never silently reopens a terminal (merged/closed) PR.
        status = pr.get('status') or 'open'
        if status == 'merged':
            payload = {'state': 'closed', 'merged': True, 'head': {'sha': head}}
        elif status == 'closed':
            payload = {'state': 'closed', 'head': {'sha': head}}
        else:
            payload = {'state': status, 'head': {'sha': head}}
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = payload
        return provider, owner, repo, number, response

    with mock.patch.dict(
        'os.environ',
        {'FORGEJO_TOKEN': 'ci-test-token', 'GITHUB_TOKEN': 'ci-test-token',
         'ORIGIN_URL': 'https://origin.shizuha',
         'PULSE_SUBSTRATE_CAP_MODE': 'off'},
        clear=True,
    ), mock.patch('tasks.tasks._linked_pr_api_request', side_effect=live_request):
        yield


@contextlib.contextmanager
def _provider_fails(item, *, mode):
    """Simulate a LIVE confirmation FAILURE for every linked PR.  ``mode`` is one
    of: ``no_token`` (env cleared of tokens — the code early-returns all rows
    unconfirmed), ``http_401`` / ``http_500`` (non-2xx response), ``exception``
    (the provider request raises), ``invalid_json`` (the response body is not
    parseable JSON).  For every non-``no_token`` mode a valid FORGEJO_TOKEN is
    kept in env so the code proceeds to the fetch and fails there."""
    if mode == 'no_token':
        with mock.patch.dict(
            'os.environ', {'ORIGIN_URL': 'https://origin.shizuha'}, clear=True
        ):
            yield
        return

    def live_request(url, **_kwargs):
        m = re.search(r'/([^/]+)/([^/]+)/pulls?/(\d+)', url)
        owner, repo, number = m.groups() if m else ('owner', 'repo', '0')
        provider = 'github' if 'github.com' in url else 'forgejo'
        if mode == 'exception':
            raise requests.RequestException('simulated provider failure')
        if mode in ('http_401', 'http_500'):
            status = 401 if mode == 'http_401' else 500
            response = mock.Mock(ok=False, status_code=status)
            response.json.return_value = {'state': 'open', 'head': {'sha': 'ignored'}}
        elif mode == 'invalid_json':
            response = mock.Mock(ok=True, status_code=200)
            response.json.side_effect = ValueError('bad')
        else:
            raise AssertionError(f'unknown _provider_fails mode: {mode}')
        return provider, owner, repo, number, response

    with mock.patch.dict(
        'os.environ',
        {'FORGEJO_TOKEN': 'ci-test-token', 'GITHUB_TOKEN': 'ci-test-token',
         'ORIGIN_URL': 'https://origin.shizuha'},
        clear=True,
    ), mock.patch('tasks.tasks._linked_pr_api_request', side_effect=live_request):
        yield


class ReviewReentryRequiresProgressTests(TestCase):
    """PLAT-3159 baseline tests (adapted for PLS-441 fail-closed)."""

    def test_same_head_without_rebuttal_is_rejected(self):
        item = _item(head_sha='abc123')
        _stamp_bounce(item)

        with self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)

        self.assertIn('abc123', str(ctx.exception))
        self.assertIn('Review re-entry gate', str(ctx.exception))

    def test_head_advance_after_bounce_passes(self):
        item = _item(head_sha='abc123')
        _stamp_bounce(item)
        meta = item.metadata
        meta['pull_requests'][0]['head_sha'] = 'def456'
        item.metadata = meta
        item.save(update_fields=['metadata'])

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_author_plat202_rebuttal_after_bounce_passes(self):
        item = _item(head_sha='abc123')
        _stamp_bounce(item)
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: this finding is intentionally argued for architect arbitration.',
        )

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_no_pr_is_exempt(self):
        """Zero linked PR rows => exempt (no-PR task)."""
        WorkflowService._run_validators(_validator_transition(), _item(pull_requests=[], labels=['no-pr']))

    def test_initial_submission_with_current_pr_head_is_exempt(self):
        """PLS-456: a linked current head without a bounce is first submission."""
        item = _item(head_sha='abc123')

        WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_missing_bounce_provenance_fail_closed(self):
        """A recorded bounce without its PR-head snapshot fails closed."""
        item = _item(head_sha='abc123')
        meta = item.metadata
        meta['review_bounce_at'] = timezone.now().isoformat()
        item.metadata = meta
        item.save(update_fields=['metadata'])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('no bounce record', str(ctx.exception))

    def test_missing_head_sha_fail_closed(self):
        """Linked PRs without head_sha => fail-closed with sync-required error."""
        item = _item(pull_requests=[{
            'url': PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 3159,
            'status': 'open',
            # no head_sha
        }])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('no head_sha', str(ctx.exception))

    def test_stamp_review_bounce_records_head_and_timestamp(self):
        before = timezone.now()
        item = _item(head_sha='abc123')
        _stamp_bounce(item)

        # Single-PR: legacy scalar still written for compat
        self.assertEqual(item.metadata['review_bounce_head_sha'], 'abc123')
        self.assertEqual(item.metadata['review_bounce_pr_heads'][PR_URL], 'abc123')
        self.assertGreaterEqual(timezone.datetime.fromisoformat(item.metadata['review_bounce_at']), before)

    def test_stamp_review_bounce_multi_pr_no_scalar(self):
        """Multi-PR bounce: no lossy scalar, per-PR dict only."""
        item = _item(pull_requests=[
            {'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159, 'status': 'open', 'head_sha': 'abc'},
            {'url': PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 10, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)
        self.assertNotIn('review_bounce_head_sha', item.metadata)
        self.assertEqual(item.metadata['review_bounce_pr_heads'][PR_URL], 'abc')
        self.assertEqual(item.metadata['review_bounce_pr_heads'][PR2_URL], 'def')

    def test_stamp_review_bounce_missing_head_stored_as_null(self):
        """Missing current head stored as explicit null in bounce map."""
        item = _item(pull_requests=[{
            'url': PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 3159,
            'status': 'open',
            # no head_sha
        }])
        _stamp_bounce(item)
        self.assertIsNone(item.metadata['review_bounce_pr_heads'][PR_URL])


class Pls441MultiPrRegressionTests(TestCase):
    """PLS-441: per-PR, fail-closed provenance for multi-PR tasks."""

    def test_two_prs_no_heads_rejected(self):
        """PLAT-4205 reproduction: two linked PRs without head_sha => reject."""
        item = _item(pull_requests=[
            {'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159, 'status': 'open'},
            {'url': PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 10, 'status': 'open'},
        ])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('no head_sha', str(ctx.exception))

    def test_mixed_head_present_and_missing_after_no_pr_bounce_refuses(self):
        """PLS-851: mixed case — one linked PR with head, one without, after a
        no-PR bounce, with an explicit-scope rebuttal present — must REFUSE.

        This is the fail-open fragility: the gate must branch on the structural
        fact (a head-less linked PR exists), NOT on the rendered prose
        substring 'no head_sha'. A copy-edit of the operator-facing detail must
        never flip the gate into admitting a task with a head-less PR.
        """
        item = _item(pull_requests=[
            {'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159, 'status': 'open', 'head_sha': 'abc'},
            {'url': PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 10, 'status': 'open'},  # no head_sha
        ])
        # No-PR bounce: review_bounce_at recorded, but no PR-head snapshot, so
        # the `if not bounced_heads:` first-code-artifact branch is exercised.
        meta = item.metadata
        meta['review_bounce_at'] = timezone.now().isoformat()
        item.metadata = meta
        item.save(update_fields=['metadata'])

        # Explicit-scope rebuttal naming the PR that HAS a head — must NOT admit
        # the item while a head-less linked PR exists.
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#3159 — argued for architect arbitration.',
        )

        with _live_confirm(item):
            with self.assertRaises(WorkflowValidationError) as ctx:
                WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('no head_sha', str(ctx.exception))

    def test_two_prs_both_unchanged_rejected(self):
        """Both bounced PRs unchanged => reject."""
        item = _item(pull_requests=[
            {'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159, 'status': 'open', 'head_sha': 'abc'},
            {'url': PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 10, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)

        with self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('shizuha-labs/shizuha-tasks#3159', str(ctx.exception))
        self.assertIn('shizuha-labs/agent#10', str(ctx.exception))

    def test_two_prs_only_one_advances_rejected(self):
        """Only one PR advances => reject naming the unchanged one."""
        item = _item(pull_requests=[
            {'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159, 'status': 'open', 'head_sha': 'abc'},
            {'url': PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 10, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)

        # Advance only the first PR
        meta = item.metadata
        meta['pull_requests'][0]['head_sha'] = 'xyz'
        item.metadata = meta
        item.save(update_fields=['metadata'])

        with self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('shizuha-labs/agent#10', str(ctx.exception))
        self.assertNotIn('shizuha-labs/shizuha-tasks#3159', str(ctx.exception))

    def test_two_prs_both_advance_passes(self):
        """Both PRs advance => pass."""
        item = _item(pull_requests=[
            {'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159, 'status': 'open', 'head_sha': 'abc'},
            {'url': PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 10, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)

        meta = item.metadata
        meta['pull_requests'][0]['head_sha'] = 'xyz'
        meta['pull_requests'][1]['head_sha'] = 'uvw'
        item.metadata = meta
        item.save(update_fields=['metadata'])

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_terminal_predecessor_with_post_bounce_open_successor_admits(self):
        """v7 rule 2 + 4 (canonical v8): a provider-confirmed MERGED bounced PR is
        audit-only automatically — no structured Architecture designation needed —
        and a post-bounce open successor is progress. The task admits.

        (Was test_terminal_bounced_pr_without_structured_architect_designation_fails
        under the superseded v6 'terminal status alone is not a waiver' rule, which
        Architecture rejected in the v7 amendment.)"""
        item = _item(pull_requests=[{
            'url': PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 3159,
            'status': 'open',
            'head_sha': 'abc',
        }])
        _stamp_bounce(item)

        meta = item.metadata
        meta['pull_requests'][0]['status'] = 'merged'      # bounced predecessor now terminal
        meta['pull_requests'].append({                     # post-bounce open successor
            'url': PR2_URL,
            'repo': 'shizuha-labs/agent',
            'number': 10,
            'status': 'open',
            'head_sha': 'def',
        })
        item.metadata = meta
        item.save(update_fields=['metadata'])

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_terminal_bounced_pr_never_waives_a_different_unchanged_bounced_open_pr(self):
        """v7 rule 3/4: excluding a terminal bounced PR must NEVER waive a DIFFERENT
        unchanged bounced OPEN PR. Both PRs are bounced; the merged one is audit-only,
        but the still-open unchanged one (no rebuttal) keeps the gate closed and the
        rejection names the OPEN tuple, not the terminal one."""
        item = _item(pull_requests=[
            {'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159,
             'status': 'open', 'head_sha': 'abc'},
            {'url': PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 10,
             'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)   # BOTH are bounced obligations

        meta = item.metadata
        meta['pull_requests'][0]['status'] = 'merged'   # #3159 terminal → audit-only
        # agent#10 stays open, head unchanged, no rebuttal → still an obligation
        item.metadata = meta
        item.save(update_fields=['metadata'])

        with self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        msg = str(ctx.exception)
        self.assertIn('shizuha-labs/agent#10', msg)
        self.assertNotIn('shizuha-labs/shizuha-tasks#3159', msg)

    def test_all_terminal_task_admits_without_designation(self):
        """v7 rule 2: an all-terminal task (only bounced PR is merged/closed) is
        audit-only and admits with no structured designation — the wedge the v7
        amendment exists to clear."""
        item = _item(pull_requests=[{
            'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159,
            'status': 'open', 'head_sha': 'abc',
        }])
        _stamp_bounce(item)

        meta = item.metadata
        meta['pull_requests'][0]['status'] = 'merged'
        item.metadata = meta
        item.save(update_fields=['metadata'])

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_stale_v6_designation_cannot_poison_advanced_v7_open_tuple(self):
        """PLS-600 live recurrence (PLAT-4689): after v7, a historical
        structured designation is audit-only.  Losing Architecture roster
        authority or naming the old open head must not reject a provider-
        confirmed terminal predecessor plus a genuinely advanced open tuple.
        """
        item = _item(pull_requests=[
            {
                'url': PR_URL,
                'repo': 'shizuha-labs/shizuha-tasks',
                'number': 3159,
                'status': 'open',
                'head_sha': 'abc',
            },
            {
                'url': PR2_URL,
                'repo': 'shizuha-labs/agent',
                'number': 10,
                'status': 'open',
                'head_sha': 'def-old',
            },
        ])
        _stamp_bounce(item)

        meta = item.metadata
        meta['pull_requests'][0]['status'] = 'merged'
        meta['pull_requests'][1]['head_sha'] = 'def-new'
        item.metadata = meta
        item.save(update_fields=['metadata'])
        _architect_active_tuple_comment(
            item,
            active_url=PR2_URL,
            active_head='def-old',
        )

        with mock.patch.object(
            WorkflowService,
            '_direction_author_is_authoritative',
            return_value=False,
        ) as authoritative, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        authoritative.assert_not_called()

    def test_terminal_bounced_pr_with_unchanged_active_pr_still_blocked(self):
        """A historical v6 designation never waives another unchanged open PR."""
        item = _item(pull_requests=[
            {'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159, 'status': 'open', 'head_sha': 'abc'},
            {'url': PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 10, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)

        meta = item.metadata
        meta['pull_requests'][0]['status'] = 'merged'
        item.metadata = meta
        item.save(update_fields=['metadata'])
        _architect_active_tuple_comment(item, active_url=PR2_URL, active_head='def')

        with mock.patch.object(
            WorkflowService,
            '_direction_author_is_authoritative',
            return_value=True,
        ):
            with self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
                WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('shizuha-labs/agent#10', str(ctx.exception))
        self.assertNotIn('shizuha-labs/shizuha-tasks#3159', str(ctx.exception))

    def test_rebuttal_covers_terminal_bounced_pr_with_active_follow_up(self):
        """A scoped PLAT-202 rebuttal remains a separate author bypass path."""
        item = _item(pull_requests=[{
            'url': PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 3159,
            'status': 'open',
            'head_sha': 'abc',
        }])
        _stamp_bounce(item)

        meta = item.metadata
        meta['pull_requests'][0]['status'] = 'merged'
        meta['pull_requests'].append({
            'url': PR2_URL,
            'repo': 'shizuha-labs/agent',
            'number': 10,
            'status': 'open',
            'head_sha': 'def',
        })
        item.metadata = meta
        item.save(update_fields=['metadata'])
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#3159 — argued for architect arbitration.',
        )

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_scoped_multi_pr_rebuttal_passes(self):
        """Scoped rebuttal naming each unchanged PR passes."""
        item = _item(pull_requests=[
            {'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159, 'status': 'open', 'head_sha': 'abc'},
            {'url': PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 10, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)

        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#3159, shizuha-labs/agent#10 — both argued for architect arbitration.',
        )

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_all_linked_prs_rebuttal_passes(self):
        """'all linked PRs' rebuttal covers all unchanged PRs."""
        item = _item(pull_requests=[
            {'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159, 'status': 'open', 'head_sha': 'abc'},
            {'url': PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 10, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)

        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: all linked PRs — argued for architect arbitration.',
        )

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_unscoped_multi_pr_rebuttal_fails(self):
        """Generic rebuttal without naming unchanged PRs fails for multi-PR."""
        item = _item(pull_requests=[
            {'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159, 'status': 'open', 'head_sha': 'abc'},
            {'url': PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 10, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)

        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: this is argued for architect arbitration.',
        )

        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(_validator_transition(), item)

    def test_missing_bounce_head_fail_closed(self):
        """Bounce map has null head => fail-closed with sync-required error."""
        item = _item(pull_requests=[{
            'url': PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 3159,
            'status': 'open',
            'head_sha': 'abc',
        }])
        _stamp_bounce(item)

        # Simulate bounce map with null head
        meta = item.metadata
        meta['review_bounce_pr_heads'][PR_URL] = None
        item.metadata = meta
        item.save(update_fields=['metadata'])

        with self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('bounce head missing', str(ctx.exception))


class Pls684PartialBounceBackfillTests(TestCase):
    """PLS-684: production-order relink of PLAT-4602's partial snapshot."""

    def test_relink_is_idempotent_and_scoped_rebuttal_admits_once(self):
        pulse_server = _import_pulse_server()
        github_url = 'https://github.com/shizuha-labs/deploy/pull/494'
        origin_url = 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/7'
        legacy_head = '01df0ca0e6fec4dec98cc857018090407f30be6d'
        successor_head = 'c4ab822260c6fa05abf0e9615355d4edd8a21860'
        item = _item(pull_requests=[
            {
                'url': github_url,
                'repo': 'shizuha-labs/deploy',
                'number': 494,
                'status': 'open',
                'head_sha': legacy_head,
                'linked_at': '2026-07-13T21:31:51Z',
            },
            {
                'url': origin_url,
                'repo': 'shizuha-labs/deploy',
                'number': 7,
                'status': 'open',
                'head_sha': successor_head,
                'linked_at': '2026-07-14T20:48:54Z',
            },
        ])
        metadata = item.metadata
        metadata['review_bounce_at'] = '2026-07-14T20:49:50Z'
        metadata['review_bounce_pr_heads'] = {
            github_url: legacy_head,
            origin_url: None,
        }
        item.metadata = metadata
        item.save(update_fields=['metadata'])

        patch_count = 0

        def api_request(method, path, data=None):
            nonlocal patch_count
            current = Item.objects.get(pk=item.pk)
            if method == 'GET':
                return {
                    'id': current.pk,
                    'item_key': current.item_key,
                    'metadata': current.metadata,
                }
            if method == 'PATCH':
                patch_count += 1
                current.metadata = (data or {}).get('metadata') or {}
                current.save(update_fields=['metadata'])
                return {'id': current.pk, 'metadata': current.metadata}
            raise AssertionError(method)

        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = {
            'state': 'open',
            'merged': False,
            'head': {'sha': successor_head},
        }
        with mock.patch.dict(
            'os.environ',
            {'FORGEJO_TOKEN': 'ci-test-token', 'PULSE_SUBSTRATE_CAP_MODE': 'off'},
            clear=False,
        ), mock.patch.object(
            pulse_server, 'api_request', side_effect=api_request,
        ), mock.patch.object(
            pulse_server.requests, 'get', return_value=response,
        ):
            first = pulse_server.link_pr(item.item_key, origin_url)
            second = pulse_server.link_pr(item.item_key, origin_url)

        self.assertIn('bounce_head', first)
        self.assertEqual(second, f'PR already linked to task #{item.item_key}')
        self.assertEqual(patch_count, 1)

        item.refresh_from_db()
        self.assertEqual(
            item.metadata['review_bounce_pr_heads'],
            {github_url: legacy_head, origin_url: successor_head},
        )
        self.assertEqual(
            item.metadata['review_bounce_backfill_source'],
            'pulse_link_pr',
        )

        # Backfill repairs provenance only; it never admits an unchanged tuple
        # by itself.
        with self.assertRaises(WorkflowValidationError), _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)

        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content=(
                'PLAT-202 rebuttal: all linked PRs; exact successor '
                f'{successor_head}, mergeable=true, required Origin gates green.'
            ),
        )
        with _live_confirm(item):
            WorkflowService._run_validators(
                _validator_transition(), item
            )  # one successful production-order review re-entry


class Pls496OriginRebuttalRecognitionTests(TestCase):
    """PLS-496: the scoped ``owner/repo#N`` rebuttal recognizer must match
    Origin/Forgejo PRs (``origin.shizuha.com`` host, ``/pulls/N`` path), not
    only ``github.com`` ``/pull/N``.  Prior heads recognized Origin PRs by full
    URL only, so an author naming the Origin PR by its ``owner/repo#N`` short
    form went unrecognized and re-entry blocked incorrectly."""

    def test_origin_scoped_rebuttal_by_owner_repo_number_passes(self):
        """Two unchanged Origin PRs: naming BOTH by ``owner/repo#N`` covers
        them.  Two non-mirror PRs force the scoping path, so this only passes
        when the recognizer parses Origin ``/pulls/N`` URLs into ``owner/repo#N``
        — the exact fix this test guards (the prior github.com-only regex left
        both unnamed and blocked)."""
        item = _item(pull_requests=[
            {'url': ORIGIN_PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 22, 'status': 'open', 'head_sha': 'abc'},
            {'url': ORIGIN_PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 7, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)

        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#22, shizuha-labs/agent#7 — both argued for architect arbitration.',
        )

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_origin_full_url_rebuttal_also_passes(self):
        """The full Origin URL in the rebuttal body is accepted too (belt and
        suspenders — the recognizer matches either the short form or the URL)."""
        item = _item(pull_requests=[
            {'url': ORIGIN_PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 22, 'status': 'open', 'head_sha': 'abc'},
            {'url': ORIGIN_PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 7, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)

        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content=f'PLAT-202 rebuttal: {ORIGIN_PR_URL} and {ORIGIN_PR2_URL} — both argued for arbitration.',
        )

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_origin_unscoped_multi_pr_rebuttal_still_fails(self):
        """A generic rebuttal naming neither unchanged Origin PR still fails —
        the Origin path enforces scoping just like GitHub."""
        item = _item(pull_requests=[
            {'url': ORIGIN_PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 22, 'status': 'open', 'head_sha': 'abc'},
            {'url': ORIGIN_PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 7, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)

        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: this is argued for architect arbitration.',
        )

        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(_validator_transition(), item)

    def test_origin_scoped_rebuttal_rejects_word_or_numeric_suffix(self):
        """revi P2: owner/repo#22abc / #220 / #22_ must NOT satisfy a rebuttal
        scoped to owner/repo#22 — the number needs a right token boundary."""
        for suffix in ("abc", "0", "_x", "9"):
            item = _item(pull_requests=[
                {'url': ORIGIN_PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 22, 'status': 'open', 'head_sha': 'abc'},
                {'url': ORIGIN_PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 7, 'status': 'open', 'head_sha': 'def'},
            ])
            _stamp_bounce(item)
            Comment.objects.create(
                item=item,
                author_id=101,
                author_email='author@agents.shizuha.io',
                content=f'PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#22{suffix}, shizuha-labs/agent#7 — arbitration.',
            )
            with self.assertRaises(WorkflowValidationError):
                WorkflowService._run_validators(_validator_transition(), item)

    def test_no_pr_bounce_then_first_origin_pr_rebuttal_passes(self):
        """PLAT-4837: a no-PR review bounce has no per-PR snapshot.  When the
        first code PR is linked later with current provenance, a scoped author
        rebuttal must be evaluated rather than rejected by the old early return."""
        item = _item(pull_requests=[], labels=['no-pr'])
        _stamp_bounce(item)

        metadata = item.metadata
        metadata['pull_requests'] = [{
            'url': ORIGIN_PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 22,
            'status': 'open',
            'head_sha': 'first-code-head',
        }]
        item.metadata = metadata
        item.labels = []
        item.save(update_fields=['metadata', 'labels'])
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#22 — first code artifact addresses the review.',
        )

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)

    def test_two_origin_prs_refresh_current_heads_before_comparison(self):
        """PLAT-4615: both stored tuples are stale at their bounced heads.  The
        admission boundary refreshes both live Origin heads and evaluates the
        current pair, so real progress never needs a no-op push."""
        item = _item(pull_requests=[
            {'url': ORIGIN_PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 22, 'status': 'open', 'head_sha': 'old-one'},
            {'url': ORIGIN_PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 7, 'status': 'open', 'head_sha': 'old-two'},
        ])
        _stamp_bounce(item)
        payloads = {
            ORIGIN_PR_URL: {'state': 'open', 'head': {'sha': 'new-one'}},
            ORIGIN_PR2_URL: {'state': 'open', 'head': {'sha': 'new-two'}},
        }

        def live_request(url, **_kwargs):
            response = mock.Mock(ok=True, status_code=200)
            response.json.return_value = payloads[url]
            owner, repo, number = (
                ('shizuha-labs', 'shizuha-tasks', '22')
                if url == ORIGIN_PR_URL
                else ('shizuha-labs', 'agent', '7')
            )
            return 'forgejo', owner, repo, number, response

        with mock.patch.dict(
            'os.environ',
            {'FORGEJO_TOKEN': 'ci-test-token', 'ORIGIN_URL': 'https://origin.shizuha',
             'PULSE_SUBSTRATE_CAP_MODE': 'off'},
            clear=True,
        ), mock.patch('tasks.tasks._linked_pr_api_request', side_effect=live_request):
            WorkflowService._run_validators(_validator_transition(), item)

        item.refresh_from_db()
        self.assertEqual(
            [pr['head_sha'] for pr in item.metadata['pull_requests']],
            ['new-one', 'new-two'],
        )

    def test_post_blocker_resume_refreshes_single_origin_pr_head(self):
        """PLAT-4376: auto-resume after a blocker must compare the exact live
        fixed head, not the stale pre-block metadata tuple."""
        item = _item(pull_requests=[{
            'url': ORIGIN_PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 22,
            'status': 'open',
            'head_sha': 'pre-review-head',
        }])
        _stamp_bounce(item)
        metadata = item.metadata
        metadata['pre_block_status'] = 'in_review'
        item.metadata = metadata
        item.save(update_fields=['metadata'])

        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = {
            'state': 'open',
            'head': {'sha': 'post-review-fixed-head'},
        }
        with mock.patch.dict(
            'os.environ',
            {'FORGEJO_TOKEN': 'ci-test-token', 'ORIGIN_URL': 'https://origin.shizuha',
             'PULSE_SUBSTRATE_CAP_MODE': 'off'},
            clear=True,
        ), mock.patch(
            'tasks.tasks._linked_pr_api_request',
            return_value=('forgejo', 'shizuha-labs', 'shizuha-tasks', '22', response),
        ):
            WorkflowService._run_validators(_validator_transition(), item)

        item.refresh_from_db()
        self.assertEqual(
            item.metadata['pull_requests'][0]['head_sha'],
            'post-review-fixed-head',
        )

    def test_second_task_shared_remediation_via_real_origin_link_then_gate(self):
        """revi P1: production-order cross-layer regression. A SECOND/sibling task
        links an already-MERGED Origin PR as shared remediation via the REAL MCP
        link path (pulse_server.link_pr), which fetches head_sha + merged state
        from the Forgejo API and persists them; then the scoped rebuttal re-entry
        gate must recognize the merged shared PR by owner/repo#N. Exercises merged
        metadata ingestion AND re-entry recognition together — not a hand-mutated
        status. Retains the no-rebuttal negative below."""
        pulse_server = _import_pulse_server()
        item = _item(pull_requests=[
            {'url': ORIGIN_PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 7, 'status': 'open', 'head_sha': 'followupsha'},
        ])

        def api_request(method, path, data=None):
            it = Item.objects.get(pk=item.pk)
            if method == "GET":
                return {"id": it.pk, "item_key": it.item_key, "metadata": it.metadata}
            if method == "PATCH":
                it.metadata = (data or {}).get("metadata") or {}
                it.save(update_fields=["metadata"])
                return {"id": it.pk, "metadata": it.metadata}
            return {}

        resp = mock.Mock(status_code=200)
        resp.json.return_value = {
            "head": {"sha": "mergedsha22"},
            "state": "closed",
            "merged": True,
            "title": "shared remediation",
        }
        with mock.patch.dict("os.environ", {"FORGEJO_TOKEN": "ci-test-token"}), \
             mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server, "requests") as req:
            req.get.return_value = resp
            out = pulse_server.link_pr(item.item_key, ORIGIN_PR_URL)

        item.refresh_from_db()
        shared = [p for p in item.metadata["pull_requests"] if "shizuha-tasks" in p["url"]][0]
        self.assertEqual(shared.get("head_sha"), "mergedsha22", out)
        self.assertEqual(shared.get("status"), "merged", out)

        _stamp_bounce(item)
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email="author@agents.shizuha.io",
            content="PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#22, shizuha-labs/agent#7 — argued for arbitration.",
        )
        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_second_task_shared_remediation_via_real_origin_link_without_rebuttal_blocks(self):
        """Same real-link merged-PR ingestion, but NO rebuttal -> the merged
        shared Origin PR still blocks re-entry (terminal status is not a waiver)."""
        pulse_server = _import_pulse_server()
        item = _item(pull_requests=[
            {'url': ORIGIN_PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 7, 'status': 'open', 'head_sha': 'followupsha'},
        ])

        def api_request(method, path, data=None):
            it = Item.objects.get(pk=item.pk)
            if method == "GET":
                return {"id": it.pk, "item_key": it.item_key, "metadata": it.metadata}
            if method == "PATCH":
                it.metadata = (data or {}).get("metadata") or {}
                it.save(update_fields=["metadata"])
                return {"id": it.pk, "metadata": it.metadata}
            return {}

        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"head": {"sha": "mergedsha22"}, "state": "closed", "merged": True, "title": "x"}
        with mock.patch.dict("os.environ", {"FORGEJO_TOKEN": "ci-test-token"}), \
             mock.patch.object(pulse_server, "api_request", side_effect=api_request), \
             mock.patch.object(pulse_server, "requests") as req:
            req.get.return_value = resp
            pulse_server.link_pr(item.item_key, ORIGIN_PR_URL)

        item.refresh_from_db()
        _stamp_bounce(item)
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(_validator_transition(), item)

class GitHubWebhookHeadShaTests(TestCase):
    def test_synchronize_event_updates_linked_pr_head_sha_without_status_change(self):
        item = _item(head_sha='oldsha')
        payload = {
            'action': 'synchronize',
            'pull_request': {
                'html_url': PR_URL,
                'state': 'open',
                'merged': False,
                'head': {'sha': 'newsha'},
                'user': {'login': 'sara2574'},
            },
        }
        request = APIRequestFactory().post(
            '/api/webhooks/github/',
            data=json.dumps(payload).encode('utf-8'),
            content_type='application/json',
            HTTP_X_GITHUB_EVENT='pull_request',
        )

        with mock.patch.dict(
                'os.environ', {'PULSE_SUBSTRATE_CAP_MODE': 'off'}, clear=True):
            response = GitHubWebhookView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        pr = item.metadata['pull_requests'][0]
        self.assertEqual(pr['status'], 'open')
        self.assertEqual(pr['head_sha'], 'newsha')
        self.assertTrue(ItemActivity.objects.filter(item=item, action='pr_status_changed').exists())


# PLS-496 merge de-confliction: the retired-mirror / helper tests below were
# authored against ``/pulls/1`` and originally rebound the module-level
# ``ORIGIN_PR_URL``.  After merging the PLS-496 rebuttal-recognition block
# (which defines ORIGIN_PR_URL = ``/pulls/22`` at the top of the module and
# names PRs by ``owner/repo#22``), a second module-level ``ORIGIN_PR_URL``
# reassignment here shadowed it for the WHOLE module (Python globals are
# last-write-wins, resolved at call time), so those tests saw ``/pulls/1`` with
# a ``number: 22`` field and the scoped-rebuttal recognizer built ``#1`` — never
# matching ``#22`` in the rebuttal body.  Keep this block on its own constant.
ORIGIN_PR1_URL = 'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/1'
ORIGIN_MIRROR_URL = 'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/3159'


class ReviewReentryRetiredMirrorTests(TestCase):
    """PLAT-4675 Facet 2: a frozen retired-mirror github.com PR must not block
    re-entry once the fix is republished on an active Origin PR — but a task whose
    only linked PR is the stale GitHub one still fails closed."""

    def test_frozen_mirror_not_waived_by_unrelated_active_origin_pr(self):
        """v8 rule 5 (revi #645 P1.2): a bare active Origin PR must NOT silently
        waive a distinct frozen GitHub mirror. Without an exact per-row
        `superseded_by` mapping (or scoped arbitration), the frozen open mirror
        stays an open obligation and the gate blocks. (Was
        test_retired_github_mirror_exempt_when_active_origin_pr, which locked the
        over-broad v6 behavior the v7 amendment rejects.)"""
        item = _item(pull_requests=[{
            'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159,
            'status': 'open', 'head_sha': 'ghfrozen',
        }])
        _stamp_bounce(item)
        meta = item.metadata
        meta['pull_requests'].append({   # an UNRELATED active Origin PR, no mapping
            'url': ORIGIN_PR1_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 1,
            'status': 'open', 'head_sha': 'originfix',
        })
        item.metadata = meta
        item.save(update_fields=['metadata'])
        with self.assertRaises(WorkflowValidationError), _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)

    def test_frozen_mirror_exempt_by_exact_superseded_by_mapping(self):
        """v8 rule 5: a frozen GitHub mirror IS auto-dispositioned when THAT exact
        row carries a trusted `superseded_by` mapping to a linked, live-confirmed,
        currently-open canonical Origin tuple. Admits."""
        item = _item(pull_requests=[{
            'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159,
            'status': 'open', 'head_sha': 'ghfrozen',
            'superseded_by': ORIGIN_MIRROR_URL,   # exact trusted mapping
        }])
        _stamp_bounce(item)
        meta = item.metadata
        meta['pull_requests'].append({
            'url': ORIGIN_MIRROR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159,
            'status': 'open', 'head_sha': 'originfix',
        })
        item.metadata = meta
        item.save(update_fields=['metadata'])
        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_real_origin_link_writes_mapping_then_reader_admits(self):
        """Fork-B production order: provider-confirmed ``pulse_link_pr`` writes
        the exact mirror mapping, then the admission reader consumes it."""
        pulse_server = _import_pulse_server()
        item = _item(pull_requests=[{
            'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159,
            'status': 'open', 'head_sha': 'ghfrozen',
        }])
        _stamp_bounce(item)

        def api_request(method, path, data=None):
            current = Item.objects.get(pk=item.pk)
            if method == 'GET':
                return {'id': current.pk, 'item_key': current.item_key, 'metadata': current.metadata}
            if method == 'PATCH':
                current.metadata = (data or {}).get('metadata') or {}
                current.save(update_fields=['metadata'])
                return {'id': current.pk, 'metadata': current.metadata}
            raise AssertionError(method)

        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = {
            'state': 'open', 'merged': False, 'head': {'sha': 'originfix'},
        }
        with mock.patch.dict('os.environ', {'FORGEJO_TOKEN': 'ci-test-token'}), \
             mock.patch.object(pulse_server, 'api_request', side_effect=api_request), \
             mock.patch.object(pulse_server.requests, 'get', return_value=response):
            out = pulse_server.link_pr(item.item_key, ORIGIN_MIRROR_URL)

        item.refresh_from_db()
        github_row = next(p for p in item.metadata['pull_requests'] if p['url'] == PR_URL)
        self.assertEqual(github_row.get('superseded_by'), ORIGIN_MIRROR_URL, out)
        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_stale_github_only_still_fails_closed(self):
        item = _item(pull_requests=[{
            'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159,
            'status': 'open', 'head_sha': 'ghfrozen',
        }])
        _stamp_bounce(item)
        # No Origin PR — the stale GitHub PR is unchanged, so re-entry stays blocked.
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('Review re-entry gate', str(ctx.exception))

    def test_origin_pr_without_head_sha_does_not_exempt(self):
        """PLAT-4675 P1 (kai): the exempting Origin PR must itself carry provenance
        (a non-empty head_sha), else a bare Origin link would allow provenance-free
        re-entry — the very thing the gate prevents. Stays blocked."""
        item = _item(pull_requests=[{
            'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159,
            'status': 'open', 'head_sha': 'ghfrozen',
        }])
        _stamp_bounce(item)
        # An Origin PR is linked but has NO head_sha (provenance-free).
        meta = item.metadata
        meta['pull_requests'].append({
            'url': ORIGIN_PR1_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 1,
            'status': 'open',  # deliberately no head_sha
        })
        item.metadata = meta
        item.save(update_fields=['metadata'])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('Review re-entry gate', str(ctx.exception))
        # helper: a provenance-free Origin PR grants NO exemption
        self.assertEqual(
            WorkflowService._review_reentry_retired_mirror_keys(item, {PR_URL: 'ghfrozen'}), set())

    def test_retired_mirror_keys_helper(self):
        """v8 rule 5 (revi #645 P1.2): the helper exempts a frozen GitHub key ONLY
        when that exact row carries a `superseded_by` mapping to a linked,
        live-confirmed, currently-open canonical Origin tuple. All other shapes —
        missing mapping, evil-host mapping, unlinked/unconfirmed/unrelated/terminal
        target — fail closed (∅)."""
        gh = lambda **kw: {'url': PR_URL, 'status': 'open', 'head_sha': 'x',
                           'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159, **kw}
        origin_open = {'url': ORIGIN_MIRROR_URL, 'status': 'open', 'head_sha': 'y',
                       'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159}
        confirmed = {ORIGIN_MIRROR_URL: True}

        def call(prs, refresh=None):
            item = _item(pull_requests=prs)
            by_url = {(p.get('url') or '').rstrip('/'): p for p in prs}
            return WorkflowService._review_reentry_retired_mirror_keys(
                item, {PR_URL: 'x'}, all_prs_by_url=by_url,
                refresh_confirmed=refresh if refresh is not None else confirmed)

        # POSITIVE: exact mapping to a linked, confirmed, open Origin tuple.
        self.assertEqual(call([gh(superseded_by=ORIGIN_MIRROR_URL), origin_open]), {PR_URL})
        # NEGATIVE — no mapping at all (the over-broad v6 case: active Origin ≠ waiver).
        self.assertEqual(call([gh(), origin_open]), set())
        # NEGATIVE — evil/look-alike host in the mapping (unparseable → fail closed).
        self.assertEqual(call([gh(superseded_by='https://origin.shizuha.com.evil.com/o/r/pulls/1'),
                               origin_open]), set())
        # NEGATIVE — target not linked to the task.
        self.assertEqual(call([gh(superseded_by=ORIGIN_MIRROR_URL)]), set())
        # NEGATIVE — target linked+open but NOT live-confirmed this admission.
        self.assertEqual(call([gh(superseded_by=ORIGIN_MIRROR_URL), origin_open], refresh={}), set())
        # NEGATIVE — strict short keys are valid link references but never valid
        # stored supersession values; the writer always stores a canonical URL.
        self.assertEqual(call([gh(superseded_by='shizuha-labs/shizuha-tasks#3159'), origin_open]), set())
        # NEGATIVE — mapping points at a DIFFERENT Origin tuple than the linked one.
        other = 'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/99'
        self.assertEqual(call([gh(superseded_by=other), origin_open]), set())
        # NEGATIVE — target linked but terminal (merged), not open.
        merged_origin = {**origin_open, 'status': 'merged'}
        self.assertEqual(call([gh(superseded_by=ORIGIN_MIRROR_URL), merged_origin]), set())
        # NEGATIVE — target linked+open but provenance-free (no head_sha).
        bare_origin = {'url': ORIGIN_PR1_URL, 'status': 'open',
                       'repo': 'shizuha-labs/shizuha-tasks', 'number': 1}
        self.assertEqual(call([gh(superseded_by=ORIGIN_MIRROR_URL), bare_origin]), set())

    def test_lookalike_origin_host_does_not_exempt(self):
        """PLAT-4675 P1 (kai review): the exempting Origin PR must be an EXACT
        canonical origin.shizuha.com URL. A look-alike host that merely CONTAINS
        the string (origin.shizuha.com.evil.com) — even with a head_sha — must NOT
        grant the retired-mirror exemption; the frozen GitHub link keeps blocking."""
        lookalike = 'https://origin.shizuha.com.evil.com/shizuha-labs/shizuha-tasks/pulls/1'
        item = _item(pull_requests=[{
            'url': PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 3159,
            'status': 'open', 'head_sha': 'ghfrozen',
        }])
        _stamp_bounce(item)
        meta = item.metadata
        meta['pull_requests'].append({
            'url': lookalike, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 1,
            'status': 'open', 'head_sha': 'lookalikeprovenance',
        })
        item.metadata = meta
        item.save(update_fields=['metadata'])
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('Review re-entry gate', str(ctx.exception))
        self.assertEqual(
            WorkflowService._review_reentry_retired_mirror_keys(item, {PR_URL: 'ghfrozen'}), set())

    def test_noncanonical_origin_urls_do_not_exempt(self):
        for url in (
            'http://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/1',
            'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/1?x=1',
            'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/1#x',
        ):
            with self.subTest(url=url):
                item = _item(pull_requests=[
                    {'url': PR_URL, 'status': 'open', 'head_sha': 'ghfrozen'},
                    {'url': url, 'status': 'open', 'head_sha': 'not-provenance'},
                ])
                self.assertEqual(
                    WorkflowService._review_reentry_retired_mirror_keys(
                        item, {PR_URL: 'ghfrozen'}
                    ),
                    set(),
                )


_FAIL_MODES = ('no_token', 'http_401', 'http_500', 'exception', 'invalid_json')


class Pls496LiveConfirmationFailClosedTests(TestCase):
    """revi P1.2 / v3 rule 8: a review-obligated PR whose LIVE provider head
    cannot be confirmed at admission fails closed — even when the STORED head
    looks advanced, and even when a valid scoped PLAT-202 rebuttal covers it.
    A provider/auth/parse failure never admits from a stale stored tuple."""

    def _bounced_two_origin_item(self):
        item = _item(pull_requests=[
            {'url': ORIGIN_PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 22, 'status': 'open', 'head_sha': 'abc'},
            {'url': ORIGIN_PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 7, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)
        return item

    def test_advanced_stored_head_still_fails_closed_when_unconfirmed(self):
        """Scenario (a): one stored head is ALREADY different from its bounce
        head (would look advanced), but no live confirmation => fail-closed."""
        for mode in _FAIL_MODES:
            with self.subTest(mode=mode):
                item = self._bounced_two_origin_item()
                meta = item.metadata
                meta['pull_requests'][0]['head_sha'] = 'advanced-live-looking'
                item.metadata = meta
                item.save(update_fields=['metadata'])

                with self.assertRaises(WorkflowValidationError) as ctx, _provider_fails(item, mode=mode):
                    WorkflowService._run_validators(_validator_transition(), item)
                self.assertIn('unconfirmed', str(ctx.exception))

    def test_unchanged_with_valid_rebuttal_still_fails_closed_when_unconfirmed(self):
        """Scenario (b): stored heads UNCHANGED plus a valid scoped PLAT-202
        rebuttal covering both PRs — still fail-closed when unconfirmed."""
        for mode in _FAIL_MODES:
            with self.subTest(mode=mode):
                item = self._bounced_two_origin_item()
                Comment.objects.create(
                    item=item,
                    author_id=101,
                    author_email='author@agents.shizuha.io',
                    content='PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#22, shizuha-labs/agent#7 — argued for arbitration.',
                )

                with self.assertRaises(WorkflowValidationError) as ctx, _provider_fails(item, mode=mode):
                    WorkflowService._run_validators(_validator_transition(), item)
                self.assertIn('unconfirmed', str(ctx.exception))


class Pls496SinglePrExplicitScopeTests(TestCase):
    """revi P1.3: a no-PR bounce whose first code PR is linked later is
    single-PR, but ``require_explicit_scope`` disables the single-PR generic
    shortcut — a generic rebuttal that does not name the PR must NOT admit; only
    an explicit ``owner/repo#N`` (or URL / all-linked) rebuttal does."""

    def _no_pr_bounce_then_first_origin_pr(self):
        item = _item(pull_requests=[], labels=['no-pr'])
        _stamp_bounce(item)
        metadata = item.metadata
        metadata['pull_requests'] = [{
            'url': ORIGIN_PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 22,
            'status': 'open',
            'head_sha': 'first-code-head',
        }]
        item.metadata = metadata
        item.labels = []
        item.save(update_fields=['metadata', 'labels'])
        return item

    def test_generic_unscoped_single_pr_rebuttal_still_rejected(self):
        item = self._no_pr_bounce_then_first_origin_pr()
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: disagreed',
        )
        with self.assertRaises(WorkflowValidationError), _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)

    def test_explicitly_named_single_pr_rebuttal_passes(self):
        item = self._no_pr_bounce_then_first_origin_pr()
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#22 — first code artifact addresses the review.',
        )
        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise


class Pls496RebuttalScanFailClosedTests(TestCase):
    """revi P1: a comment-query failure cannot manufacture rebuttal proof.

    Cover both admission sites that depend on the helper: the first code PR
    linked after a no-PR bounce and a normal unchanged bounced PR.
    """

    @staticmethod
    def _raise_on_comment_filter(item):
        return mock.patch.object(
            type(item.comments),
            'filter',
            side_effect=RuntimeError('simulated rebuttal comment scan failure'),
        )

    def test_no_pr_first_artifact_stays_blocked_when_rebuttal_scan_fails(self):
        item = _item(pull_requests=[], labels=['no-pr'])
        _stamp_bounce(item)
        metadata = item.metadata
        metadata['pull_requests'] = [{
            'url': ORIGIN_PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 22,
            'status': 'open',
            'head_sha': 'first-code-head',
        }]
        item.metadata = metadata
        item.labels = []
        item.save(update_fields=['metadata', 'labels'])
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content=(
                'PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#22 '
                '— first code artifact addresses the review.'
            ),
        )

        with self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item), \
                self._raise_on_comment_filter(item):
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('no bounce record', str(ctx.exception))

    def test_unchanged_pr_stays_blocked_when_rebuttal_scan_fails(self):
        item = _item(pull_requests=[{
            'url': ORIGIN_PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 22,
            'status': 'open',
            'head_sha': 'unchanged-head',
        }])
        _stamp_bounce(item)
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: unchanged head is intentionally defended.',
        )

        with self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item), \
                self._raise_on_comment_filter(item):
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('head unchanged-he', str(ctx.exception))


class Pls496ArchitectureArbitrationTests(TestCase):
    """Architecture/Merge may record a scoped unchanged-head arbitration.

    Exact tuple scope is mandatory on the Architecture/Merge path — including
    the single-PR case. A generic or unrelated-tuple marker from an
    authoritative architect must stay blocked. The durable-author single-PR
    generic shortcut is intentionally preserved. The narrow no-PR
    first-artifact carve-out remains author-only.
    """

    def _unchanged_origin_pr(self):
        item = _item(pull_requests=[{
            'url': ORIGIN_PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 22,
            'status': 'open',
            'head_sha': 'unchanged-head',
        }])
        _stamp_bounce(item)
        return item

    def _pls414_mixed_terminal_item(self, *, legacy_478_status='merged'):
        """Mirror PLS-414's production tuple order after the ORIG-18 cutover.

        GitHub #478 is historical and lacks a truthful bounce snapshot, GitHub
        #480 is the unchanged open obligation, and canonical Origin #83 is
        merged audit history.  The helper links all three, stamps the two active
        legacy obligations, then removes only #478's historical bounce head so
        the regression exercises the real terminal-before-provenance ordering
        boundary.
        """
        legacy_478 = (
            'https://github.com/shizuha-labs/shizuha-tasks/pull/478'
        )
        legacy_480 = (
            'https://github.com/shizuha-labs/shizuha-tasks/pull/480'
        )
        origin_83 = (
            'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/83'
        )
        item = _item(pull_requests=[
            {
                'url': legacy_478,
                'repo': 'shizuha-labs/shizuha-tasks',
                'number': 478,
                'status': 'open',
                'head_sha': 'legacy-478-head',
            },
            {
                'url': legacy_480,
                'repo': 'shizuha-labs/shizuha-tasks',
                'number': 480,
                'status': 'open',
                'head_sha': 'legacy-480-head',
            },
            {
                'url': origin_83,
                'repo': 'shizuha-labs/shizuha-tasks',
                'number': 83,
                'status': 'merged',
                'head_sha': 'origin-83-head',
            },
        ])
        _stamp_bounce(item)

        metadata = item.metadata
        metadata['review_bounce_pr_heads'][legacy_478] = None
        metadata['pull_requests'][0]['status'] = legacy_478_status
        item.metadata = metadata
        item.save(update_fields=['metadata'])
        return item, legacy_478, legacy_480

    def test_pls414_terminal_missing_bounce_then_scoped_open_arbitration_admits(self):
        """PLS-680 production order: live-confirmed terminal #478 is audit-only
        even without a bounce head; exact Architecture arbitration then admits
        the sole unchanged open obligation (#480)."""
        item, _legacy_478, _legacy_480 = self._pls414_mixed_terminal_item()
        Comment.objects.create(
            item=item,
            author_id=202,
            author_email='sora@shizuha.com',
            content=(
                'PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#480 '
                '— Architecture arbitration accepts this unchanged open tuple.'
            ),
        )

        with mock.patch.object(
            WorkflowService, '_direction_author_is_authoritative', return_value=True
        ), _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)

    def test_pls414_open_missing_bounce_still_fails_closed(self):
        """The same missing bounce head remains fatal when the live provider
        confirms #478 is still open, even though #480 has exact arbitration."""
        item, legacy_478, _legacy_480 = self._pls414_mixed_terminal_item(
            legacy_478_status='open',
        )
        Comment.objects.create(
            item=item,
            author_id=202,
            author_email='sora@shizuha.com',
            content=(
                'PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#480 '
                '— Architecture arbitration accepts this unchanged open tuple.'
            ),
        )

        with mock.patch.object(
            WorkflowService, '_direction_author_is_authoritative', return_value=True
        ), self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn(legacy_478, str(ctx.exception))
        self.assertIn('bounce head missing', str(ctx.exception))

    def test_pls414_terminal_missing_bounce_does_not_waive_unscoped_open_tuple(self):
        """Terminal exclusion is narrow: without exact #480 arbitration, the
        remaining unchanged open obligation is still rejected."""
        item, legacy_478, legacy_480 = self._pls414_mixed_terminal_item()
        Comment.objects.create(
            item=item,
            author_id=202,
            author_email='sora@shizuha.com',
            content='PLAT-202 rebuttal: Architecture accepts the remediation.',
        )

        with mock.patch.object(
            WorkflowService, '_direction_author_is_authoritative', return_value=True
        ), self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        message = str(ctx.exception)
        self.assertIn('shizuha-labs/shizuha-tasks#480', message)
        self.assertNotIn(legacy_478, message)
        self.assertNotIn('bounce head missing', message)
        self.assertNotIn(legacy_480, message)

    def test_scoped_architecture_arbitration_admits_unchanged_head(self):
        item = self._unchanged_origin_pr()
        Comment.objects.create(
            item=item,
            author_id=202,
            author_email='aoi@shizuha.com',
            content=(
                'PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#22 '
                '— architect arbitration accepts the unchanged remediation head.'
            ),
        )

        with mock.patch.object(
            WorkflowService, '_direction_author_is_authoritative', return_value=True
        ) as authoritative, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertEqual(
            authoritative.call_args.args[1],
            'aoi@shizuha.com',
        )
        self.assertEqual(
            authoritative.call_args.args[2]['direction_teams'],
            ['architecture', 'merge'],
        )

    def test_authoritative_generic_architecture_arbitration_stays_blocked(self):
        """revi PLS-496 P1: architect + generic marker must NOT waive single-PR."""
        item = self._unchanged_origin_pr()
        Comment.objects.create(
            item=item,
            author_id=202,
            author_email='aoi@shizuha.com',
            content='PLAT-202 rebuttal: accepted',
        )

        with mock.patch.object(
            WorkflowService, '_direction_author_is_authoritative', return_value=True
        ), self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('head unchanged-he', str(ctx.exception))

    def test_authoritative_unrelated_tuple_architecture_arbitration_stays_blocked(self):
        """revi PLS-496 P1: architect naming a non-obligation tuple grants nothing."""
        item = self._unchanged_origin_pr()
        Comment.objects.create(
            item=item,
            author_id=202,
            author_email='aoi@shizuha.com',
            # Body must NOT also name the real obligation (owner/repo#N, bare
            # repo#N, or URL) — that would satisfy scope and hide the negative.
            content=(
                'PLAT-202 rebuttal: shizuha-labs/agent#7 '
                '— waive this unrelated predecessor only.'
            ),
        )

        with mock.patch.object(
            WorkflowService, '_direction_author_is_authoritative', return_value=True
        ), self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        self.assertIn('head unchanged-he', str(ctx.exception))

    def test_durable_author_single_pr_generic_rebuttal_still_admits(self):
        """Architecture explicit-scope rule must not strip the author shortcut."""
        item = self._unchanged_origin_pr()
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: accepted — defending this single head.',
        )

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_non_authoritative_third_party_rebuttal_stays_blocked(self):
        item = self._unchanged_origin_pr()
        Comment.objects.create(
            item=item,
            author_id=303,
            author_email='outsider@example.com',
            content='PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#22 — waive.',
        )

        with mock.patch.object(
            WorkflowService, '_direction_author_is_authoritative', return_value=False
        ), self.assertRaises(WorkflowValidationError), _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)

    def test_architecture_cannot_replace_author_on_no_pr_first_artifact(self):
        item = _item(pull_requests=[], labels=['no-pr'])
        _stamp_bounce(item)
        metadata = item.metadata
        metadata['pull_requests'] = [{
            'url': ORIGIN_PR_URL,
            'repo': 'shizuha-labs/shizuha-tasks',
            'number': 22,
            'status': 'open',
            'head_sha': 'first-code-head',
        }]
        item.metadata = metadata
        item.labels = []
        item.save(update_fields=['metadata', 'labels'])
        Comment.objects.create(
            item=item,
            author_id=202,
            author_email='aoi@shizuha.com',
            content='PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#22 — arbitration.',
        )

        with mock.patch.object(
            WorkflowService, '_direction_author_is_authoritative', return_value=True
        ), self.assertRaises(WorkflowValidationError), _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)


class Pls496ScopeLeftBoundaryTests(TestCase):
    """revi P2: the scoped ``owner/repo#N`` recognizer needs a LEFT token
    boundary too — a prefix/embedded-glued lookalike (``evilshizuha-labs/...``,
    ``.../shizuha-labs/...``) must NOT satisfy a rebuttal scoped to the real
    ``shizuha-labs/shizuha-tasks#22``; the properly delimited token still does."""

    def _two_unchanged_origin_prs(self):
        item = _item(pull_requests=[
            {'url': ORIGIN_PR_URL, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 22, 'status': 'open', 'head_sha': 'abc'},
            {'url': ORIGIN_PR2_URL, 'repo': 'shizuha-labs/agent', 'number': 7, 'status': 'open', 'head_sha': 'def'},
        ])
        _stamp_bounce(item)
        return item

    def test_left_glued_lookalike_does_not_satisfy_scope(self):
        # #7 named cleanly; #22 only present prefix-glued / path-embedded, so the
        # scope for #22 is unsatisfied and the gate must RAISE.
        for glued in (
            'evilshizuha-labs/shizuha-tasks#22',          # alpha prefix-glued
            'x/shizuha-labs/shizuha-tasks#22',            # path/slash-embedded
        ):
            with self.subTest(glued=glued):
                item = self._two_unchanged_origin_prs()
                Comment.objects.create(
                    item=item,
                    author_id=101,
                    author_email='author@agents.shizuha.io',
                    content=f'PLAT-202 rebuttal: {glued}, shizuha-labs/agent#7 — arbitration.',
                )
                with self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
                    WorkflowService._run_validators(_validator_transition(), item)
                self.assertIn('shizuha-labs/shizuha-tasks#22', str(ctx.exception))

    def test_properly_delimited_scope_still_passes(self):
        item = self._two_unchanged_origin_prs()
        Comment.objects.create(
            item=item,
            author_id=101,
            author_email='author@agents.shizuha.io',
            content='PLAT-202 rebuttal: shizuha-labs/shizuha-tasks#22, shizuha-labs/agent#7 — arbitration.',
        )
        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise


# ---------------------------------------------------------------------------
# PLS-840 / Standard v21 rule 14 — the exact `no-pr` sentinel is a declaration,
# never a PR obligation.
#
# The pairing is the point throughout: each admission test has a refusal twin on
# the same fixture shape.  A sentinel exclusion that only ever admits is
# indistinguishable from having deleted the gate, and rule 14's own wording is
# explicit that "the sentinel waives nothing and never authorizes the code merge
# path".
# ---------------------------------------------------------------------------

NO_PR_ROW = {'url': 'no-pr', 'status': 'open'}


def _genuine_row(head_sha='gen-head', number=9001, status='open'):
    return {
        'url': f'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/{number}',
        'repo': 'shizuha-labs/shizuha-tasks',
        'number': number,
        'status': status,
        'head_sha': head_sha,
    }


class Pls840NoPrSentinelContractTests(TestCase):
    """v21 rule 14 regression lock."""

    # ---- sentinel-only: the PLAT-5519 shape -------------------------------

    def test_sentinel_only_task_reenters_after_a_bounce(self):
        """Production order: bounce with only the sentinel linked, then re-enter."""
        item = _item(pull_requests=[dict(NO_PR_ROW)])
        _stamp_bounce(item)
        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_sentinel_row_alone_makes_a_task_reviewable_without_the_flag(self):
        """v21: the row counts as the declaration 'even if a historical writer
        omitted the flag'.  Carries no label/tag deliberately."""
        item = _item(pull_requests=[dict(NO_PR_ROW)], labels=[])
        self.assertTrue(WorkflowService._is_reviewable(item))

    def test_sentinel_never_authorizes_the_code_merge_path(self):
        """The refusal twin of the test above — same fixture, opposite gate.

        Reviewability and merge-authorization are different questions and the
        sentinel answers them oppositely.
        """
        item = _item(pull_requests=[dict(NO_PR_ROW)], labels=[])
        self.assertFalse(WorkflowService._has_linked_pr(item))

    # ---- the sentinel is never a tuple or a bounce key ---------------------

    def test_bounce_stamping_never_keys_the_sentinel(self):
        item = _item(pull_requests=[dict(NO_PR_ROW), _genuine_row()])
        _stamp_bounce(item)
        heads = item.metadata.get('review_bounce_pr_heads') or {}
        self.assertNotIn('no-pr', heads)
        self.assertEqual(len(heads), 1, f'only the genuine row may be stamped: {heads}')

    def test_sentinel_is_absent_from_current_head_maps(self):
        item = _item(pull_requests=[dict(NO_PR_ROW), _genuine_row()])
        self.assertNotIn('no-pr', WorkflowService._current_linked_pr_heads(item))

    # ---- P1#2: a LEGACY stamped sentinel key must not wedge a real PR ------

    def test_legacy_sentinel_bounce_key_does_not_wedge_a_later_genuine_pr(self):
        """The regression @rei found: a task bounced BEFORE the stamping fix.

        Its stored map already contains {'no-pr': None}.  Left unfiltered, that
        key alone keeps bounced_heads non-empty, skipping the rule 7
        first-code-artifact branch and comparing a key that can never be
        live-confirmed — wedging the first GENUINE PR forever.
        """
        item = _item(pull_requests=[dict(NO_PR_ROW)])
        meta = dict(item.metadata)
        meta['review_bounce_at'] = timezone.now().isoformat()
        meta['review_bounce_pr_heads'] = {'no-pr': None}   # legacy stamp
        meta['pull_requests'] = [dict(NO_PR_ROW), _genuine_row()]  # real PR linked later
        item.metadata = meta
        item.save(update_fields=['metadata'])

        Comment.objects.create(
            item=item, author_id=101, author_email='author@agents.shizuha.io',
            content=(
                'PLAT-202 rebuttal scoped to '
                'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/9001'
            ),
        )
        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    # ---- the anti-bypass twin --------------------------------------------

    def test_sentinel_does_not_waive_an_unchanged_genuine_obligation(self):
        item = _item(pull_requests=[dict(NO_PR_ROW), _genuine_row()])
        _stamp_bounce(item)
        with self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        detail = str(ctx.exception)
        self.assertIn('9001', detail)
        # `no-pr (`, not bare `no-pr`: this detail comes from _run_validators,
        # which itself emits the legitimate advice "add the 'no-pr' label to
        # declare that explicitly". A bare needle would fail on that correct
        # output and report it as a sentinel leak — the failure naming the
        # opposite of its cause. See the twin in test_pls840_no_pr_marker_reentry.
        self.assertNotIn('no-pr (', detail, 'the sentinel must not appear in remediation text')

    def test_sentinel_plus_advanced_genuine_pr_follows_the_genuine_tuple(self):
        item = _item(pull_requests=[dict(NO_PR_ROW), _genuine_row(head_sha='old')])
        _stamp_bounce(item)
        meta = dict(item.metadata)
        meta['pull_requests'][1]['head_sha'] = 'advanced'
        item.metadata = meta
        item.save(update_fields=['metadata'])
        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    # ---- lookalikes stay fail-closed --------------------------------------

    def test_lookalike_sentinels_remain_ordinary_unsatisfiable_obligations(self):
        for row in (
            {'url': 'no-pr', 'repo': 'shizuha-labs/x', 'number': 7, 'status': 'open'},
            {'url': 'no-pr-really', 'status': 'open'},
            {'url': 'https://example.test/no-pr', 'status': 'open'},
            {'url': 'no‑pr', 'status': 'open'},   # U+2011 non-breaking hyphen
            {'url': 'nopr', 'status': 'open'},
        ):
            with self.subTest(url=row['url']):
                self.assertFalse(WorkflowService._is_no_pr_marker(row))
                item = _item(pull_requests=[dict(row)])
                _stamp_bounce(item)
                with self.assertRaises(WorkflowValidationError), _live_confirm(item):
                    WorkflowService._run_validators(_validator_transition(), item)


GHOST_PR_URL = 'https://origin.shizuha.com/shizuha-labs/shizuha/pulls/27'


class Plat9403UnlinkedGhostBounceKeyTests(TestCase):
    """PLAT-9403: a bounce key whose linked row was removed by
    ``pulse_unlink_pr`` must not be cited by the re-entry gate.

    Live failure (SCLI-582): the gate rejected every retry naming a verified
    ghost PR (provider 404) AFTER ``pulse_unlink_pr`` archived its row to
    ``metadata.removed_pull_requests`` — the bounce snapshot
    ``review_bounce_pr_heads`` retains the key, the row-driven refresh loop can
    never confirm it, and the fail-closed ``unconfirmed`` branch held the task
    forever. Contract locked here:
    - a removed row carries NO re-entry obligation — the gate must not name it;
    - a removed ghost never excuses a DIFFERENT still-linked unchanged
      obligation — the gate still fails closed on that row;
    - audit history is never rewritten: the archived row stays in
      ``metadata.removed_pull_requests``.
    """

    def _item_with_ghost_and_live_row(self):
        return _item(pull_requests=[
            {
                'url': GHOST_PR_URL,
                'repo': 'shizuha-labs/shizuha',
                'number': 27,
                'status': 'open',
                'head_sha': 'ghost-head',
            },
            {
                'url': ORIGIN_PR_URL,
                'repo': 'shizuha-labs/shizuha-tasks',
                'number': 22,
                'status': 'open',
                'head_sha': 'same-head',
            },
        ])

    def _unlink_ghost(self, item):
        """Simulate the real pulse_unlink_pr mutation: pop the row from
        ``pull_requests``, archive it under ``removed_pull_requests``."""
        meta = dict(item.metadata)
        prs = meta['pull_requests']
        idx = next(
            i for i, r in enumerate(prs)
            if str(r.get('url') or '').rstrip('/') == GHOST_PR_URL
        )
        removed = dict(prs.pop(idx))
        removed['removed_at'] = '2026-09-19T20:00:00Z'
        removed['removed_reason'] = 'verified ghost (provider 404)'
        removed['removed_by'] = 'pulse_unlink_pr'
        meta['pull_requests'] = prs
        meta['removed_pull_requests'] = (meta.get('removed_pull_requests') or []) + [removed]
        item.metadata = meta
        item.save(update_fields=['metadata', 'updated_at'])
        item.refresh_from_db()

    def test_unlinked_ghost_bounce_key_is_not_cited_after_retry(self):
        item = self._item_with_ghost_and_live_row()
        _stamp_bounce(item)
        self.assertIn(GHOST_PR_URL, item.metadata['review_bounce_pr_heads'])
        self._unlink_ghost(item)
        # The remaining row advanced past its bounce head and is live-confirmed.
        meta = dict(item.metadata)
        meta['pull_requests'][0]['head_sha'] = 'advanced-head'
        item.metadata = meta
        item.save(update_fields=['metadata'])

        with _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)  # must not raise

    def test_unlinked_ghost_does_not_excuse_unchanged_remaining_row(self):
        item = self._item_with_ghost_and_live_row()
        _stamp_bounce(item)
        self._unlink_ghost(item)

        with self.assertRaises(WorkflowValidationError) as ctx, _live_confirm(item):
            WorkflowService._run_validators(_validator_transition(), item)
        detail = str(ctx.exception)
        # Fail-closed on the REAL remaining obligation (the gate labels rows as
        # owner/repo#N, not the full URL)…
        self.assertIn('shizuha-labs/shizuha-tasks#22', detail)
        # …and the ghost must not be cited in either form (the defect: it was,
        # verbatim, as "live provider head unconfirmed").
        self.assertNotIn(GHOST_PR_URL, detail)
        self.assertNotIn('shizuha-labs/shizuha#27', detail)

    def test_unlink_audit_history_is_preserved(self):
        item = self._item_with_ghost_and_live_row()
        _stamp_bounce(item)
        self._unlink_ghost(item)
        archive = item.metadata['removed_pull_requests']
        self.assertEqual(len(archive), 1)
        self.assertEqual(archive[0]['url'], GHOST_PR_URL)
        self.assertEqual(archive[0]['removed_by'], 'pulse_unlink_pr')
