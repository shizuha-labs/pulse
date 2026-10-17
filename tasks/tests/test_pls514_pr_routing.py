"""PLS-514: a PR must become routed work even when its task cannot route it.

The two cases this exists for, neither reachable by an author-side prompt:
  * the PR has no linked Pulse task at all (alert-driven on-call fix);
  * the task's workflow has no transition to `in_review` (Simple Workflow).

The safety property is as load-bearing as the feature: this creates Pulse tasks
from an external webhook across repos holding >200 open PRs, so "off unless
explicitly enabled for a named repo" is tested first and directly.
"""
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Item
from tasks.services import pr_routing


def _env(enabled='1', repos='shizuha-labs/shizuha-tasks'):
    return patch.dict('os.environ', {
        'PULSE_PR_ROUTING_ENABLED': enabled,
        'PULSE_PR_ROUTING_REPOS': repos,
    })


def _pr(**kw):
    base = dict(
        repo='shizuha-labs/shizuha-tasks',
        number=140,
        url='https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/140',
        title='restore main to green',
    )
    base.update(kw)
    return base


class PrRoutingGuardTests(TestCase):
    """The guards, first — a runaway task-creator is the expensive failure."""

    def test_disabled_by_default_creates_nothing(self):
        """No env at all must be inert, not merely quiet."""
        with patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(pr_routing.ensure_pr_routing_item(**_pr()))
        self.assertEqual(Item.objects.count(), 0)

    def test_master_switch_is_load_bearing_ON_ITS_OWN(self):
        """Isolate the kill switch from the allowlist guard.

        `test_disabled_by_default_creates_nothing` clears the whole environment,
        so the allowlist is empty too and the call returns None via the ALLOWLIST
        guard. Deleting the master switch therefore leaves that test green — a
        mutation run proved exactly that. This pins the switch with the repo
        explicitly allowlisted, so the switch is the only thing saying no.
        """
        with _env(enabled='0', repos='shizuha-labs/shizuha-tasks'):
            self.assertIsNone(pr_routing.ensure_pr_routing_item(**_pr()))
        self.assertEqual(Item.objects.count(), 0)

    def test_allowlist_is_load_bearing_ON_ITS_OWN(self):
        """Mirror image: switch ON, repo absent — only the allowlist declines."""
        with _env(enabled='1', repos='shizuha-labs/unrelated'):
            self.assertIsNone(pr_routing.ensure_pr_routing_item(**_pr()))
        self.assertEqual(Item.objects.count(), 0)

    def test_enabled_but_repo_not_allowlisted_creates_nothing(self):
        with _env(repos='shizuha-labs/some-other-repo'):
            self.assertIsNone(pr_routing.ensure_pr_routing_item(**_pr()))
        self.assertEqual(Item.objects.count(), 0)

    def test_draft_is_not_routed(self):
        with _env():
            self.assertIsNone(pr_routing.ensure_pr_routing_item(**_pr(draft=True)))
        self.assertEqual(Item.objects.count(), 0)


class PrRoutingCreateTests(TestCase):
    def test_POSITIVE_CONTROL_enabled_and_allowlisted_creates_a_routed_item(self):
        """The detector must be observed FIRING on a known-good input.

        Every guard test above passes against a function that does nothing at
        all; this is the one that distinguishes 'correctly declines' from
        'never works'.
        """
        with _env():
            item = pr_routing.ensure_pr_routing_item(**_pr())
        self.assertIsNotNone(item)
        self.assertEqual(Item.objects.count(), 1)
        self.assertEqual(item.assignment_group, 'review')
        self.assertEqual(item.status, 'open')
        self.assertEqual(item.metadata['pr_routing_repo'], 'shizuha-labs/shizuha-tasks')
        self.assertEqual(item.metadata['pr_routing_number'], 140)
        self.assertIn('#140', item.title)

    def test_merge_kind_routes_to_the_merge_team_not_review(self):
        with _env():
            item = pr_routing.ensure_pr_routing_item(**_pr(kind=pr_routing.KIND_MERGE))
        self.assertEqual(item.assignment_group, 'merge')
        self.assertIn('[merge]', item.title)

    def test_lands_on_a_team_queue_not_a_status_needing_another_transition(self):
        """PLS-514 must not re-create the defect one layer up.

        If the generated item could itself sit unrouted, this reproduces lane B
        for the routing items themselves.
        """
        with _env():
            item = pr_routing.ensure_pr_routing_item(**_pr())
        self.assertTrue(item.assignment_group, 'must carry an assignment_group')
        self.assertNotIn(item.status, {'needs_triage', 'backlog'})


class PrRoutingDedupeTests(TestCase):
    def test_repeat_events_refresh_one_item_rather_than_creating_more(self):
        """`synchronize` storms and webhook redelivery must not multiply items."""
        with _env():
            first = pr_routing.ensure_pr_routing_item(**_pr())
            for _ in range(5):
                again = pr_routing.ensure_pr_routing_item(**_pr(title='updated title'))
        self.assertEqual(Item.objects.count(), 1)
        self.assertEqual(again.pk, first.pk)
        self.assertIn('updated title', again.title)

    def test_a_different_pr_number_is_a_different_item(self):
        """Negative control for the dedupe key — it must not collapse everything."""
        with _env():
            pr_routing.ensure_pr_routing_item(**_pr(number=140))
            pr_routing.ensure_pr_routing_item(**_pr(number=141))
        self.assertEqual(Item.objects.count(), 2)


class PrRoutingLifecycleTests(TestCase):
    def test_merged_pr_resolves_the_item(self):
        with _env():
            item = pr_routing.ensure_pr_routing_item(**_pr())
            pr_routing.ensure_pr_routing_item(**_pr(merged=True, state='closed'))
        item.refresh_from_db()
        self.assertEqual(item.status, 'done')
        self.assertEqual(item.metadata['pr_routing_resolved_reason'], 'merged')

    def test_closed_unmerged_pr_also_resolves(self):
        with _env():
            item = pr_routing.ensure_pr_routing_item(**_pr())
            pr_routing.ensure_pr_routing_item(**_pr(state='closed'))
        item.refresh_from_db()
        self.assertEqual(item.status, 'done')
        self.assertEqual(item.metadata['pr_routing_resolved_reason'], 'closed')

    def test_reopened_pr_revives_the_same_item_and_does_not_mint_a_second(self):
        """A resolved item must be revived, not duplicated.

        Searching only non-terminal items for the dedupe key would silently
        create a second item every reopen cycle.
        """
        with _env():
            first = pr_routing.ensure_pr_routing_item(**_pr())
            pr_routing.ensure_pr_routing_item(**_pr(merged=True, state='closed'))
            revived = pr_routing.ensure_pr_routing_item(**_pr(state='open'))
        self.assertEqual(Item.objects.count(), 1)
        self.assertEqual(revived.pk, first.pk)
        self.assertEqual(revived.status, 'open')

    def test_resolving_an_unknown_pr_is_a_no_op(self):
        with _env():
            self.assertIsNone(
                pr_routing.resolve_pr_routing_item(
                    'shizuha-labs/shizuha-tasks', 999, reason='merged'))


class PrRoutingAlreadyLinkedTests(TestCase):
    """@hiro's second negative control (PLAT-5050).

    The whole justification for PR-sourced routing is the class of PRs whose
    author *cannot* fire Submit for Review. A PR that already has a task can be
    routed by its author through the normal path, so minting an item for it
    duplicates work — and since most PRs are task-first, an unguarded version
    would create far more noise than signal.
    """

    def _task_linking(self, url, number=140):
        # repo/number must agree with the url — the guard matches on either,
        # so an inconsistent fixture tests the wrong thing (it did: the first
        # cut passed a /999 url while asserting number=140 and the fallback
        # correctly matched 140).
        return Item.objects.create(
            title='some author-owned task',
            status='open',
            mode='task',
            metadata={'pull_requests': [{
                'url': url, 'repo': 'shizuha-labs/shizuha-tasks',
                'number': number, 'status': 'open',
            }]},
        )

    def test_pr_with_an_existing_linked_task_is_NOT_routed(self):
        self._task_linking(_pr()['url'])
        with _env():
            self.assertIsNone(pr_routing.ensure_pr_routing_item(**_pr()))
        # only the author's task exists — no routing item was minted
        self.assertEqual(Item.objects.count(), 1)

    def test_CONTROL_same_call_DOES_route_when_no_task_links_it(self):
        """Isolates the new guard.

        Without this, the assertion above passes against a function that has
        stopped routing entirely — which is exactly the failure mode the
        master-switch test already caught once in this file.
        """
        with _env():
            item = pr_routing.ensure_pr_routing_item(**_pr())
        self.assertIsNotNone(item)
        self.assertEqual(Item.objects.count(), 1)

    def test_a_task_linking_a_DIFFERENT_pr_does_not_suppress_routing(self):
        """The guard must match on this PR, not on 'any task has any PR'."""
        self._task_linking('https://origin.shizuha.com/shizuha-labs/shizuha-tasks/pulls/999', number=999)
        with _env():
            self.assertIsNotNone(pr_routing.ensure_pr_routing_item(**_pr()))

    def test_our_own_routing_item_is_not_read_as_an_existing_task(self):
        """A refresh must not decline because it found its own artifact."""
        with _env():
            first = pr_routing.ensure_pr_routing_item(**_pr())
            again = pr_routing.ensure_pr_routing_item(**_pr(title='updated'))
        self.assertEqual(Item.objects.count(), 1)
        self.assertEqual(again.pk, first.pk)
        self.assertIn('updated', again.title)
