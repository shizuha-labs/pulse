"""PLS-490: scoped PLAT-202 rebuttals must accept owner/repo#N for Origin keys.

The rejection message renders unchanged PRs as ``owner/repo#N`` labels, but the
scoped-rebuttal matcher only accepted that form for github.com keys — Origin
keys silently required the full PR URL, steering authors into a format the gate
rejected (HIVE-1153: four rejected resubmissions of a reviewer-directed
partial-tuple update; PLS-482 Architecture-route recurrence). The matcher now
accepts the full URL, owner/repo#N, and — when exactly one linked tuple carries
that repo basename + number — an unambiguous bare `repo#N` / `repo #N` label
(HIVE-613 evidence shape), all with boundary-aware matching (no number-prefix
or malformed suffix/path/query/fragment false positives), and the rejection
detail states the accepted format. Rebuttal authority is bound to the validated
versioned implementation-return context (plus legacy `metadata.implementer`);
the bounce-time assignee stamp was removed because it recorded the reviewer.
"""
import itertools
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from tasks.models import Comment, Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService

ORIGIN_A = 'https://origin.shizuha.com/shizuha-labs/hive/pulls/33'
ORIGIN_B = 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/77'
ORIGIN_C = 'https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/39'
GH_URL = 'https://github.com/shizuha-labs/hive/pull/322'
AUTHOR = 'author@agents.shizuha.io'
_key_counter = itertools.count(1)
_PLS490_PROJECT = None


def _pls490_project():
    """Exact-org project + engineering roster so versioned return context validates."""
    global _PLS490_PROJECT
    if _PLS490_PROJECT is not None:
        try:
            _PLS490_PROJECT.refresh_from_db()
            return _PLS490_PROJECT
        except Project.DoesNotExist:
            _PLS490_PROJECT = None
    project = Project.objects.create(
        name='PLS490 Rebuttal',
        key=f'P490{next(_key_counter)}',
        org_id=1,
        access_level='open',
    )
    # Roster requires owned @shizuha.com domain; alias matches AUTHOR via
    # _agent_email_aliases (agents.shizuha.io ↔ shizuha.com).
    TeamRoster.objects.update_or_create(
        organization_id=project.org_id,
        team_name='engineering',
        defaults={
            'organization_slug': 'shizuha',
            'tiers': [['author@shizuha.com', 'reviewer@shizuha.com']],
            'is_active': True,
        },
    )
    WorkflowService._invalidate_team_roster_caches()
    _PLS490_PROJECT = project
    return project


def _item(pull_requests, bounced_heads):
    return Item.objects.create(
        item_key=f'PLS-490-T{next(_key_counter)}',
        title='pls490 rebuttal format',
        project=_pls490_project(),
        status='in_progress',
        workflow_name='autonomous-dev',
        workflow_status='in_progress',
        assignment_group='engineering',
        assignee_id=101,
        assignee_email=AUTHOR,
        created_by_id=101,
        created_by_email=AUTHOR,
        metadata={
            'pull_requests': pull_requests,
            'review_bounce_at': '2026-07-13T00:00:00Z',
            'review_bounce_pr_heads': bounced_heads,
        },
    )


def _rebuttal(item, body):
    Comment.objects.create(
        item=item, author_id=101, author_email=AUTHOR, content=body,
    )


def _progress(item):
    """Exercise rebuttal parsing with every stored PR tuple confirmed live.

    PLS-496 made live provider confirmation a separate fail-closed prerequisite
    of the re-entry gate.  These PLS-490 fixtures isolate rebuttal syntax and
    authority, so keep that orthogonal prerequisite deterministic instead of
    depending on credentials/network state.
    """
    confirmed = {}
    for pr in WorkflowService._linked_pr_entries(item):
        key = (
            str(pr.get('url') or '').strip().rstrip('/')
            or str(pr.get('number') or pr.get('repo') or '')
        )
        confirmed[key] = True
    with mock.patch.object(
        WorkflowService,
        '_refresh_review_reentry_pr_metadata',
        return_value=(False, confirmed),
    ):
        return WorkflowService._review_reentry_has_progress(item)


def _tuple_item():
    """HIVE-1153 shape: consumer PR advanced; two Origin PRs frozen by the
    reviewer's instruction (bounced heads unchanged)."""
    return _item(
        pull_requests=[
            {'url': ORIGIN_A, 'repo': 'shizuha-labs/hive', 'number': 33,
             'status': 'open', 'head_sha': 'frozen-head-a'},
            {'url': ORIGIN_B, 'repo': 'shizuha-labs/deploy', 'number': 77,
             'status': 'open', 'head_sha': 'frozen-head-b'},
            {'url': ORIGIN_C, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 39,
             'status': 'open', 'head_sha': 'advanced-head-c'},
        ],
        bounced_heads={
            ORIGIN_A: 'frozen-head-a',
            ORIGIN_B: 'frozen-head-b',
            ORIGIN_C: 'old-head-c',
        },
    )


def _short_tuple_item():
    """Same tuple as ``_tuple_item``, persisted with strict Origin short keys."""
    short_a = 'shizuha-labs/hive#33'
    short_b = 'shizuha-labs/deploy#77'
    short_c = 'shizuha-labs/shizuha-tasks#39'
    return _item(
        pull_requests=[
            {'url': short_a, 'repo': 'shizuha-labs/hive', 'number': 33,
             'status': 'open', 'head_sha': 'frozen-head-a'},
            {'url': short_b, 'repo': 'shizuha-labs/deploy', 'number': 77,
             'status': 'open', 'head_sha': 'frozen-head-b'},
            {'url': short_c, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 39,
             'status': 'open', 'head_sha': 'advanced-head-c'},
        ],
        bounced_heads={
            short_a: 'frozen-head-a',
            short_b: 'frozen-head-b',
            short_c: 'old-head-c',
        },
    )


class PLS490RebuttalFormatTests(TestCase):
    def test_owner_repo_number_labels_cover_origin_keys(self):
        # The exact format the rejection message prints must satisfy the matcher.
        item = _tuple_item()
        _rebuttal(item, (
            'PLAT-202 rebuttal: shizuha-labs/hive#33 and shizuha-labs/deploy#77 '
            'are frozen by reviewer instruction; the fix rides the advanced PR.'
        ))
        allowed, detail = _progress(item)
        self.assertTrue(allowed, detail)

    def test_full_origin_urls_still_cover(self):
        item = _tuple_item()
        _rebuttal(item, (
            f'PLAT-202 rebuttal: {ORIGIN_A} and {ORIGIN_B} are frozen by '
            'reviewer instruction.'
        ))
        allowed, detail = _progress(item)
        self.assertTrue(allowed, detail)

    def test_full_origin_urls_with_trailing_slash_cover(self):
        # revi round 7 P1: parse_origin_pr_url accepts one canonical trailing
        # slash, so the comment matcher must accept the same representation
        # followed by either whitespace or ordinary prose punctuation. Keep
        # that comment representation equivalent when Pulse persisted the
        # linked tuples as strict short keys instead of canonical URLs.
        bodies = (
            f'PLAT-202 rebuttal: {ORIGIN_A}/ and {ORIGIN_B}/ stay frozen.',
            f'PLAT-202 rebuttal: {ORIGIN_A}/, {ORIGIN_B}/.',
        )
        for item_factory, body in itertools.product(
            (_tuple_item, _short_tuple_item), bodies,
        ):
            with self.subTest(storage=item_factory.__name__, body=body):
                item = item_factory()
                _rebuttal(item, body)
                allowed, detail = _progress(item)
                self.assertTrue(allowed, detail)

    def test_unambiguous_bare_repo_labels_cover(self):
        # revi PR #82 P1: the Architecture-upheld HIVE-613 evidence names the
        # tuples as "Connect #138" / "deploy #480" — bare `repo #N` labels
        # (case-insensitive, optional space) must cover when exactly one linked
        # tuple carries that repo basename + number.
        item = _tuple_item()
        _rebuttal(item, (
            'PLAT-202 rebuttal: Hive #33 and deploy#77 remain frozen by '
            'reviewer instruction; the fix rides the advanced PR.'
        ))
        allowed, detail = _progress(item)
        self.assertTrue(allowed, detail)

    def test_short_key_rows_accept_same_unambiguous_bare_labels(self):
        # revi round 5 P1: strict owner/repo#N rows and canonical URL rows are
        # first-class representations of the same trusted Origin coordinates.
        item = _short_tuple_item()
        _rebuttal(item, (
            'PLAT-202 rebuttal: Hive #33 and deploy#77 remain frozen by '
            'reviewer instruction; the fix rides the advanced PR.'
        ))
        allowed, detail = _progress(item)
        self.assertTrue(allowed, detail)

    def test_ambiguous_bare_repo_label_grants_nothing(self):
        # Two linked tuples share basename+number (different owners): the bare
        # form is ambiguous and must not cover either — full URL or owner form
        # required.
        origin_x = 'https://origin.shizuha.com/team-a/hive/pulls/5'
        origin_y = 'https://origin.shizuha.com/team-b/hive/pulls/5'
        item = _item(
            pull_requests=[
                {'url': origin_x, 'repo': 'team-a/hive', 'number': 5,
                 'status': 'open', 'head_sha': 'frozen-x'},
                {'url': origin_y, 'repo': 'team-b/hive', 'number': 5,
                 'status': 'open', 'head_sha': 'frozen-y'},
            ],
            # BOTH unchanged => scoped rebuttal must name each; the bare
            # 'hive #5' label matches two distinct tuples and grants nothing.
            bounced_heads={origin_x: 'frozen-x', origin_y: 'frozen-y'},
        )
        _rebuttal(item, 'PLAT-202 rebuttal: hive #5 stays frozen.')
        allowed, _detail = _progress(item)
        self.assertFalse(allowed)

    def test_short_key_rows_reject_same_ambiguous_bare_label(self):
        # Representation parity includes the fail-closed collision rule.
        short_x = 'team-a/hive#5'
        short_y = 'team-b/hive#5'
        item = _item(
            pull_requests=[
                {'url': short_x, 'repo': 'team-a/hive', 'number': 5,
                 'status': 'open', 'head_sha': 'frozen-x'},
                {'url': short_y, 'repo': 'team-b/hive', 'number': 5,
                 'status': 'open', 'head_sha': 'frozen-y'},
            ],
            bounced_heads={short_x: 'frozen-x', short_y: 'frozen-y'},
        )
        _rebuttal(item, 'PLAT-202 rebuttal: hive #5 stays frozen.')
        allowed, _detail = _progress(item)
        self.assertFalse(allowed)

    def test_malformed_or_host_bearing_short_keys_fail_closed(self):
        # Exact text, a bare label, and "all linked PRs" must not turn an
        # untrusted key into Origin provenance. The shared parser accepts only
        # canonical URLs or exact owner/repo#N keys.
        for key in (
            'origin.shizuha.com/team-a/hive#5',
            'https://evil.example/team-a/hive#5',
        ):
            with self.subTest(key=key):
                item = _item(
                    pull_requests=[
                        {'url': key, 'repo': 'team-a/hive', 'number': 5,
                         'status': 'open', 'head_sha': 'frozen-x'},
                    ],
                    bounced_heads={key: 'frozen-x'},
                )
                _rebuttal(
                    item,
                    f'PLAT-202 rebuttal: {key}; hive #5; all linked PRs.',
                )
                allowed, _detail = _progress(item)
                self.assertFalse(allowed)

    def test_number_prefix_label_does_not_cover(self):
        # revi round 4 P1 #2: substring matching accepted `hive #330` /
        # `deploy#770` for unchanged hive#33 / deploy#77 because the required
        # label is a prefix of a different PR number. Boundary matching must
        # reject this.
        item = _tuple_item()
        _rebuttal(item, (
            'PLAT-202 rebuttal: hive #330 and deploy#770 stay frozen per '
            'reviewer instruction.'
        ))
        allowed, _detail = _progress(item)
        self.assertFalse(allowed)

    def test_containing_repo_token_does_not_cover(self):
        # A longer repo token containing the bare alias (`myhive#33`) must not
        # satisfy the `hive#33` tuple.
        item = _tuple_item()
        _rebuttal(item, (
            'PLAT-202 rebuttal: myhive#33 and mydeploy#77 stay frozen per '
            'reviewer instruction.'
        ))
        allowed, _detail = _progress(item)
        self.assertFalse(allowed)

    def test_url_prefix_of_longer_pr_number_does_not_cover(self):
        # The full-URL form also boundary-checks: a comment naming
        # .../pulls/330 must not satisfy the unchanged .../pulls/33 key.
        item = _tuple_item()
        _rebuttal(item, (
            f'PLAT-202 rebuttal: {ORIGIN_A}0 and {ORIGIN_B}0 stay frozen per '
            'reviewer instruction.'
        ))
        allowed, _detail = _progress(item)
        self.assertFalse(allowed)

    def test_reference_suffix_lookalikes_do_not_cover(self):
        # revi round 6 P1: rejecting only a following digit still let a
        # malformed longer token satisfy exact scope. Exercise each admitted
        # representation and URL path/query/fragment continuations; the other
        # frozen tuple remains exact so each subtest isolates the lookalike.
        malformed_hive_references = (
            'hive#33evil',
            'hive#33.evil',
            'hive#33,evil',
            'hive#33-evil',
            'hive#33?view=files',
            'hive#33#discussion',
            'shizuha-labs/hive#33x',
            'shizuha-labs/hive#33.evil',
            'shizuha-labs/hive#33?view=files',
            'shizuha-labs/hive#33#discussion',
            f'{ORIGIN_A}/evil',
            f'{ORIGIN_A}//',
            f'{ORIGIN_A}.evil',
            f'{ORIGIN_A}?view=files',
            f'{ORIGIN_A}#discussion',
        )
        for malformed in malformed_hive_references:
            with self.subTest(reference=malformed):
                item = _tuple_item()
                _rebuttal(item, (
                    f'PLAT-202 rebuttal: {malformed} and deploy#77 stay frozen.'
                ))
                allowed, _detail = _progress(item)
                self.assertFalse(allowed)

    def test_exact_references_allow_normal_prose_punctuation(self):
        # Tight suffix validation must not require authors to put references on
        # separate lines. Bare, owner-qualified and canonical URL forms remain
        # valid when followed by ordinary prose punctuation.
        bodies = (
            'PLAT-202 rebuttal: hive#33, deploy#77.',
            'PLAT-202 rebuttal: (shizuha-labs/hive#33); '
            'shizuha-labs/deploy#77!',
            f'PLAT-202 rebuttal: {ORIGIN_A}? {ORIGIN_B}.',
        )
        for body in bodies:
            with self.subTest(body=body):
                item = _tuple_item()
                _rebuttal(item, body)
                allowed, detail = _progress(item)
                self.assertTrue(allowed, detail)

    def test_partial_naming_still_rejected(self):
        # Naming only ONE of two unchanged PRs must not cover both.
        item = _tuple_item()
        _rebuttal(item, 'PLAT-202 rebuttal: shizuha-labs/hive#33 is frozen.')
        allowed, _detail = _progress(item)
        self.assertFalse(allowed)

    def test_rejection_detail_states_accepted_format(self):
        item = _tuple_item()  # no rebuttal at all
        allowed, detail = _progress(item)
        self.assertFalse(allowed)
        self.assertIn('owner/repo#N', detail)
        self.assertIn('all linked PRs', detail)

    def test_github_key_labels_unchanged(self):
        # GitHub keys accepted owner/repo#N before; behavior preserved.
        item = _item(
            pull_requests=[
                {'url': GH_URL, 'repo': 'shizuha-labs/hive', 'number': 322,
                 'status': 'open', 'head_sha': 'gh-frozen'},
                {'url': ORIGIN_C, 'repo': 'shizuha-labs/shizuha-tasks', 'number': 39,
                 'status': 'open', 'head_sha': 'advanced-head-c'},
            ],
            bounced_heads={GH_URL: 'gh-frozen', ORIGIN_C: 'old-head-c'},
        )
        _rebuttal(item, 'PLAT-202 rebuttal: shizuha-labs/hive#322 stays frozen.')
        allowed, detail = _progress(item)
        self.assertTrue(allowed, detail)

    def test_all_linked_prs_phrase_still_covers(self):
        item = _tuple_item()
        _rebuttal(item, 'PLAT-202 rebuttal: applies to all linked PRs.')
        allowed, detail = _progress(item)
        self.assertTrue(allowed, detail)


class PLS490DurableRebuttalAuthorityTests(TestCase):
    """revi PR #82 P1 #2: rebuttal authority must survive the author → reviewer
    assignee handoff. `item.assignee_email` mutates to the reviewer on Submit
    for Review; an admitted implementer rebuttal must stay visible when the
    gate is re-evaluated later in the round (e.g. at Review Passed)."""

    def test_stamp_never_records_an_author_identity(self):
        # revi round 4 P1 #1: stamp_review_bounce runs while the item is
        # REVIEWER-held (it precedes assign_implementer in Request Changes),
        # so any identity captured here would be the reviewer — fail-open.
        # The stamp must not write an author field and must clear stale ones.
        item = _tuple_item()
        item.assignee_email = 'reviewer@agents.shizuha.io'
        meta = dict(item.metadata)
        meta['review_bounce_author_email'] = 'stale-reviewer@agents.shizuha.io'
        item.metadata = meta
        WorkflowService._stamp_review_bounce(item)
        self.assertNotIn('review_bounce_author_email', item.metadata)

    def test_rebuttal_survives_assignee_handoff_to_reviewer(self):
        # Full production order: implementer identity captured by the VALIDATED
        # versioned return context (stash_implementer) -> implementer posts the
        # covering rebuttal -> Submit hands the item to the reviewer (assignee
        # mutates) -> the gate re-evaluates and the implementer's rebuttal must
        # still be authoritative.
        item = _tuple_item()
        meta = dict(item.metadata)
        meta[WorkflowService._IMPLEMENTATION_RETURN_CONTEXT_KEY] = {
            'version': WorkflowService._IMPLEMENTATION_RETURN_CONTEXT_VERSION,
            'implementation_team': 'engineering',
            'implementer': {'id': 101, 'email': AUTHOR},
            'review_generation': 1,
        }
        item.metadata = meta
        item.save(update_fields=['metadata'])
        _rebuttal(item, (
            'PLAT-202 rebuttal: shizuha-labs/hive#33 and shizuha-labs/deploy#77 '
            'stay frozen per reviewer instruction.'
        ))
        # Pre-handoff: passes.
        allowed, detail = _progress(item)
        self.assertTrue(allowed, detail)
        # Handoff: the reviewer becomes assignee AND creator differs too —
        # only the durable bounce-time identity still covers the comment.
        # Raw update avoids org-team assignee validation (handoff simulation).
        # Handoff to a different roster member (reviewer) so org-team guard
        # stays green; authority must still come from the durable context.
        Item.objects.filter(pk=item.pk).update(
            assignee_email='reviewer@shizuha.com',
            assignee_id=202,
            created_by_email='someone-else@shizuha.com',
        )
        item.refresh_from_db()
        allowed, detail = _progress(item)
        self.assertTrue(allowed, detail)

    def test_without_durable_identity_handoff_would_hide_rebuttal(self):
        # Control proving the mechanism: with NO bounce-time identity recorded
        # and both live identities changed, the same rebuttal is invisible.
        item = _tuple_item()
        _rebuttal(item, (
            'PLAT-202 rebuttal: shizuha-labs/hive#33 and shizuha-labs/deploy#77 '
            'stay frozen per reviewer instruction.'
        ))
        Item.objects.filter(pk=item.pk).update(
            assignee_email='reviewer@shizuha.com',
            assignee_id=202,
            created_by_email='someone-else@shizuha.com',
        )
        item.refresh_from_db()
        allowed, _detail = _progress(item)
        self.assertFalse(allowed)


class PLS490RealSequenceAuthorityTests(TestCase):
    """reika round-2 P1 #2: exercise the REAL top-level transition sequence —
    reviewer-held item -> Request Changes bounce (stamp_review_bounce +
    assign_implementer post-functions) -> implementer rebuttal -> Submit for
    Review changes owner -> Review-Passed re-evaluation — asserting the
    rebuttal remains authoritative via the VALIDATED implementation-return
    context (reika round-2 P1 #1), not a hand-written metadata stamp."""

    IMPLEMENTER = 'impl@shizuha.com'
    REVIEWER = 'rev@shizuha.com'

    def setUp(self):
        from tasks.models import (
            Project, Status, TeamRoster, Workflow, WorkflowStatus,
            WorkflowTransition,
        )
        self.project = Project.objects.create(
            name='PLS490 seq', key='P490', org_id=1,
        )
        for team, member in (('engineering', self.IMPLEMENTER), ('review', self.REVIEWER)):
            TeamRoster.objects.update_or_create(
                organization_id=1, team_name=team,
                defaults={
                    'organization_slug': 'shizuha', 'tiers': [[member]],
                    'is_active': True, 'wip_enforced': False,
                },
            )
        s_prog, _ = Status.objects.get_or_create(
            slug='in_progress', defaults={'name': 'In Progress', 'category': 'in_progress'},
        )
        s_rev, _ = Status.objects.get_or_create(
            slug='in_review', defaults={'name': 'In Review', 'category': 'in_progress'},
        )
        self.workflow = Workflow.objects.create(
            name='PLS490 Seq WF', slug='pls490-seq', initial_status=s_prog,
        )
        WorkflowStatus.objects.create(workflow=self.workflow, status=s_prog, position=0)
        WorkflowStatus.objects.create(workflow=self.workflow, status=s_rev, position=1)
        self.submit = WorkflowTransition.objects.create(
            workflow=self.workflow, name='Submit for Review',
            from_status='in_progress', to_status='in_review', position=0,
            validators=[{'type': 'review_reentry_requires_progress'}],
            post_functions=[
                {'type': 'stash_implementer'},
                {'type': 'assign_team', 'value': 'review'},
            ],
        )
        self.request_changes = WorkflowTransition.objects.create(
            workflow=self.workflow, name='Request Changes',
            from_status='in_review', to_status='in_progress', position=1,
            validators=[],
            post_functions=[
                {'type': 'stamp_review_bounce'},
                {'type': 'assign_implementer'},
            ],
        )
        self.item = Item.objects.create(
            project=self.project,
            item_key='P490-SEQ-1',
            mode='task',
            item_type='task.generic',
            title='real-sequence authority',
            status='in_progress',
            workflow_status='in_progress',
            workflow_name=self.workflow.slug,
            assignee_id=301,
            assignee_email=self.IMPLEMENTER,
            assignment_group='engineering',
            created_by_id=999,
            created_by_email='someone-else@agents.shizuha.io',
            tags='no-pr',
            metadata={
                'pull_requests': [
                    {'url': ORIGIN_A, 'repo': 'shizuha-labs/hive', 'number': 33,
                     'status': 'open', 'head_sha': 'frozen-head-a'},
                    {'url': ORIGIN_B, 'repo': 'shizuha-labs/deploy', 'number': 77,
                     'status': 'open', 'head_sha': 'frozen-head-b'},
                ],
            },
        )

    def test_full_bounce_rebuttal_resubmit_and_reevaluation(self):
        from unittest import mock
        with mock.patch(
            'tasks.services.workflow_service.WorkflowService.'
            '_member_can_execute_from_status', return_value=True,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService.'
            '_seat_satisfies_required_capability', return_value=True,
        ), mock.patch.object(
            WorkflowService, 'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            # 1. Implementer submits — stash_implementer writes the VALIDATED
            #    return context; assign_team hands the item to the reviewer.
            WorkflowService.execute_transition(
                self.item, 'in_review', user=None,
                transition_id=str(self.submit.id),
            )
            self.item.refresh_from_db()
            ctx = (self.item.metadata or {}).get('implementation_return_context') or {}
            self.assertEqual(
                (ctx.get('implementer') or {}).get('email'), self.IMPLEMENTER,
            )
            self.assertEqual(self.item.assignment_group, 'review')

            # 2. Reviewer bounces — REAL post-functions stamp the bounce and
            #    return the item to the implementer.
            WorkflowService.execute_transition(
                self.item, 'in_progress', user=None,
                transition_id=str(self.request_changes.id),
            )
            self.item.refresh_from_db()
            self.assertTrue(
                (self.item.metadata or {}).get('review_bounce_pr_heads'),
            )

            # The bounce group-queued the return (the harness has no Origin
            # author lookup, so assign_implementer typed-held it with no
            # concrete assignee). The implementer picks the returned item
            # back up — as resolver/Start Progress does in production —
            # before rebutting and resubmitting; without an owner, submit's
            # stash_implementer would correctly refuse to arm a new context.
            self.item.assignee_id = 301
            self.item.assignee_email = self.IMPLEMENTER
            self.item.save(update_fields=['assignee_id', 'assignee_email'])

            # 3. Implementer posts the covering scoped rebuttal.
            Comment.objects.create(
                item=self.item, author_id=301, author_email=self.IMPLEMENTER,
                content=(
                    'PLAT-202 rebuttal: shizuha-labs/hive#33 and '
                    'shizuha-labs/deploy#77 stay frozen per reviewer instruction.'
                ),
            )

            # 4. Resubmit — the re-entry validator must honor the rebuttal
            #    (both bounced heads unchanged) through the REAL gate.
            with mock.patch.object(
                WorkflowService,
                '_refresh_review_reentry_pr_metadata',
                return_value=(False, {ORIGIN_A: True, ORIGIN_B: True}),
            ):
                WorkflowService.execute_transition(
                    self.item, 'in_review', user=None,
                    transition_id=str(self.submit.id),
                )
            self.item.refresh_from_db()
            self.assertEqual(self.item.status, 'in_review')

            # 5. Review-Passed re-evaluation: pin the reviewer-held state the
            #    review lane produces (concrete seat resolution is live-seat
            #    machinery outside this fix) — with assignee AND creator both
            #    non-implementer, only the validated return-context identity
            #    keeps the rebuttal visible.
            self.item.assignee_email = self.REVIEWER
            self.item.assignee_id = 302
            self.item.save(update_fields=['assignee_email', 'assignee_id'])
            self.item.refresh_from_db()
            self.assertNotEqual(self.item.assignee_email, self.IMPLEMENTER)
            allowed, detail = _progress(self.item)
            self.assertTrue(allowed, detail)

    def test_reviewer_is_not_rebuttal_authority_without_valid_context(self):
        # revi round 4 P1 #1: with the versioned return context missing (legacy
        # item), the REAL Request Changes sequence must not admit the reviewer
        # as rebuttal author. The old fallback stamped the reviewer-held
        # assignee at bounce time and then accepted their comment — fail-open.
        from unittest import mock
        with mock.patch(
            'tasks.services.workflow_service.WorkflowService.'
            '_member_can_execute_from_status', return_value=True,
        ), mock.patch(
            'tasks.services.workflow_service.WorkflowService.'
            '_seat_satisfies_required_capability', return_value=True,
        ), mock.patch.object(
            WorkflowService, 'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            # 1. Real Submit hands the item to Review.
            WorkflowService.execute_transition(
                self.item, 'in_review', user=None,
                transition_id=str(self.submit.id),
            )
            self.item.refresh_from_db()

            # 2. Simulate a legacy item: no versioned return context (and no
            #    legacy metadata.implementer either).
            meta = dict(self.item.metadata or {})
            meta.pop('implementation_return_context', None)
            meta.pop('implementer', None)
            self.item.metadata = meta
            # Reviewer-held at bounce time, exactly as in production.
            self.item.assignee_email = self.REVIEWER
            self.item.assignee_id = 302
            self.item.save(update_fields=['metadata', 'assignee_email', 'assignee_id'])

            # 3. REAL Request Changes post-functions run while reviewer-held.
            WorkflowService.execute_transition(
                self.item, 'in_progress', user=None,
                transition_id=str(self.request_changes.id),
            )
            self.item.refresh_from_db()
            # The stamp must not have admitted the reviewer as a durable author.
            self.assertNotIn(
                'review_bounce_author_email', self.item.metadata or {},
            )

            # 4. The REVIEWER posts a covering scoped rebuttal after the
            #    bounce...
            Comment.objects.create(
                item=self.item, author_id=302, author_email=self.REVIEWER,
                content=(
                    'PLAT-202 rebuttal: shizuha-labs/hive#33 and '
                    'shizuha-labs/deploy#77 stay frozen.'
                ),
            )
            # ...with the item back in the implementer's hands (production
            # state after assign_implementer returns it). This is exactly
            # where the old fallback failed open: the bounce stamp carried
            # the reviewer's email, so the reviewer's own comment satisfied
            # the gate. It must NOT be admitted as an implementer rebuttal.
            self.item.assignee_email = self.IMPLEMENTER
            self.item.assignee_id = 301
            self.item.created_by_email = 'someone-else@agents.shizuha.io'
            self.item.save(
                update_fields=['assignee_email', 'assignee_id', 'created_by_email'])
            allowed, _detail = _progress(self.item)
            self.assertFalse(allowed)
