"""PLS-198: auto-failover must fire once per (task, outage) — no duplicate
comment bursts, no per-tick re-fire, no self-failover to an alias of the down
runtime.

Encodes the four repro classes from the architecture triage:
  1. alias split-verdict (F1): every availability source is alias-folded, so
     banto@shizuha.com and banto@agents.shizuha.io can never read different
     verdicts from the same env/health source;
  2. self-failover (F3): a candidate that aliases the down identity is never a
     failover target — with no other live peer, work stays put;
  3. tick re-fire (F3/F4): once drained to a live peer, later sweep ticks make
     no further reassignment and post no further comment;
  4. concurrent-run duplicates (F2/F5): an overlapping run is skipped by the
     single-flight lock, and the comment dedupe backstop refuses to post an
     identical system comment twice within the window.
"""
import os
from unittest import mock

from django.test import TestCase

from tasks.models import Project, TeamRoster
from tasks.services.workflow_service import WorkflowService

_SWEEP_ENV = {'PULSE_HEALTH_EXPORTER_URL': '', 'PULSE_API_HEALTH_URL': 'disabled'}

BANTO_PRIMARY = 'banto@shizuha.com'
BANTO_ALIAS = 'banto@agents.shizuha.io'
NAO = 'nao@shizuha.com'


class F1AliasFoldAvailabilityTests(TestCase):
    """_configured_agent_availability must return ONE verdict per runtime identity."""

    def _availability(self, email, env, health=None, roster=None):
        with mock.patch.object(WorkflowService, '_db_agent_availability', return_value=None), \
             mock.patch.object(WorkflowService, '_health_availability_map',
                               return_value=health or {}), \
             mock.patch.object(WorkflowService, '_pod_roster',
                               return_value=roster or {'books': [[BANTO_ALIAS]]}), \
             mock.patch.dict(os.environ, env, clear=False):
            return WorkflowService._configured_agent_availability(email)

    def test_active_set_verdict_is_alias_folded(self):
        """The BKST-1 split-brain: active set names the primary spelling, roster
        carries the alias. Pre-fix the alias read False and the primary True —
        the same runtime was both drain source and failover candidate."""
        env = {'PULSE_ACTIVE_AGENT_EMAILS': BANTO_PRIMARY,
               'PULSE_UNAVAILABLE_AGENT_EMAILS': '', 'PULSE_INACTIVE_AGENT_EMAILS': '',
               'PULSE_ABSENT_AGENT_EMAILS': '', 'PULSE_AGENT_AVAILABILITY_JSON': ''}
        self.assertIs(self._availability(BANTO_ALIAS, env), True)
        self.assertIs(self._availability(BANTO_PRIMARY, env), True)

    def test_unavailable_set_verdict_is_alias_folded(self):
        env = {'PULSE_UNAVAILABLE_AGENT_EMAILS': BANTO_PRIMARY,
               'PULSE_ACTIVE_AGENT_EMAILS': '', 'PULSE_INACTIVE_AGENT_EMAILS': '',
               'PULSE_ABSENT_AGENT_EMAILS': '', 'PULSE_AGENT_AVAILABILITY_JSON': ''}
        self.assertIs(self._availability(BANTO_ALIAS, env), False)
        self.assertIs(self._availability(BANTO_PRIMARY, env), False)

    def test_health_map_hit_is_alias_folded_and_down_wins(self):
        env = {'PULSE_ACTIVE_AGENT_EMAILS': '', 'PULSE_UNAVAILABLE_AGENT_EMAILS': '',
               'PULSE_INACTIVE_AGENT_EMAILS': '', 'PULSE_ABSENT_AGENT_EMAILS': '',
               'PULSE_AGENT_AVAILABILITY_JSON': ''}
        health = {BANTO_PRIMARY: False}
        self.assertIs(self._availability(BANTO_ALIAS, env, health=health), False)
        # An explicit DOWN sample under either alias beats an UP under the other.
        health = {BANTO_PRIMARY: True, BANTO_ALIAS: False}
        self.assertIs(self._availability(BANTO_PRIMARY, env, health=health), False)

    def test_json_map_verdict_is_alias_folded(self):
        env = {'PULSE_ACTIVE_AGENT_EMAILS': '', 'PULSE_UNAVAILABLE_AGENT_EMAILS': '',
               'PULSE_INACTIVE_AGENT_EMAILS': '', 'PULSE_ABSENT_AGENT_EMAILS': '',
               'PULSE_AGENT_AVAILABILITY_JSON': '{"banto@shizuha.com": "absent"}'}
        self.assertIs(self._availability(BANTO_ALIAS, env), False)


class _SweepFixtureMixin:
    """Common roster/user fixtures for running the real sweep against sqlite."""

    USERS = {
        BANTO_PRIMARY: 112,
        BANTO_ALIAS: 112,   # both spellings resolve to ONE shizuha-id uid (PLAT-1102)
        NAO: 113,
    }

    def setUp(self):
        self.project = Project.objects.create(
            name='PLS-198 Books failover',
            key='P198',
            org_id=1,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='books',
            defaults={
                'organization_slug': 'shizuha',
                # Persist only the canonical owned-domain identity. The sweep
                # input below deliberately carries both spellings to exercise
                # alias folding at the compatibility boundary.
                'tiers': [[BANTO_PRIMARY, NAO]],
                'is_active': True,
            },
        )

    def _user_by_email(self, email):
        uid = self.USERS.get((email or '').strip().lower())
        return {'id': uid, 'email': email} if uid else None

    def _run_sweep(self, roster, availability_side_effect=None, disabled=None):
        from tasks.tasks import sweep_rebalance_pods

        patches = [
            mock.patch.object(WorkflowService, '_pod_roster', return_value=roster),
            mock.patch.object(
                WorkflowService, '_team_roster_entry_for_item',
                side_effect=lambda team, item=None: (
                    roster.get(WorkflowService._canonical_team_name(team)),
                    'shizuha',
                ),
            ),
            mock.patch('tasks.user_service.user_service.get_user_by_email',
                       side_effect=self._user_by_email),
            mock.patch.dict(os.environ, _SWEEP_ENV, clear=False),
        ]
        if availability_side_effect is not None:
            patches.append(mock.patch.object(
                WorkflowService, '_is_agent_available_for_assignment',
                side_effect=availability_side_effect))
        else:
            patches.append(mock.patch.object(
                WorkflowService, '_disabled_agent_emails',
                return_value=disabled or set()))
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            return sweep_rebalance_pods.apply(args=[], kwargs={'max_skew': 0}).get()


class F3SelfFailoverTests(_SweepFixtureMixin, TestCase):
    def test_never_fails_over_to_alias_of_down_identity(self):
        """Even under a pre-F1-style split verdict (alias down, primary 'up'),
        the drain must not move work between the two spellings of one runtime."""
        from tasks.models import Comment, Item

        item = Item.objects.create(
            title='BKST work', mode='task', status='open',
            project=self.project,
            workflow_name='autonomous-dev', assignment_group='books',
            assignee_id=112, assignee_email=BANTO_ALIAS,
        )

        def _split_verdict(email):
            return (email or '').strip().lower() != BANTO_ALIAS

        self._run_sweep({'books': [[BANTO_PRIMARY, BANTO_ALIAS]]},
                        availability_side_effect=_split_verdict)

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, BANTO_ALIAS)  # untouched
        self.assertEqual(
            Comment.objects.filter(
                item=item,
                author_email='system@pulse',
                content__startswith='Auto-failover',
            ).count(),
            0,
        )

    def test_down_list_deduped_by_identity(self):
        """Both spellings of a down runtime appear in the roster; the drain must
        move its work ONCE (one reassignment, one comment), not once per alias."""
        from tasks.models import Comment, Item

        item = Item.objects.create(
            title='BKST work', mode='task', status='open',
            project=self.project,
            workflow_name='autonomous-dev', assignment_group='books',
            assignee_id=112, assignee_email=BANTO_ALIAS,
        )

        self._run_sweep({'books': [[BANTO_PRIMARY, BANTO_ALIAS, NAO]]},
                        disabled={BANTO_ALIAS})

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, NAO)
        self.assertEqual(
            Comment.objects.filter(item=item, author_email='system@pulse',
                                   content__startswith='Auto-failover').count(), 1)


class PLS881EmptyShadowPodTests(_SweepFixtureMixin, TestCase):
    """PLS-881: a same-named EMPTY-tier org-2 roster row must not produce a
    spurious "no live peer" verdict, and the org-1 pod must still drain a
    provider-dead owner's non-terminal work to a live same-team peer in one pass.

    Live incident (2026-08-06): TWO TeamRoster rows named `admin-ops` existed —
    pk 20 (org 1, staffed) and pk 181 (org 2, tiers=[[]] EMPTY). The empty row
    logged `no live peer distinct from its down identities` on every sweep run.
    """

    DEAD = 'shizuha@shizuha.com'
    LIVE = 'goro@shizuha.com'
    USERS = {DEAD: 140, LIVE: 432}

    def setUp(self):
        self.project = Project.objects.create(
            name='PLS-881 admin-ops failover',
            key='P881',
            org_id=1,
        )
        # org-1 admin-ops: the provider-dead owner + a live same-team peer.
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='admin-ops',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.DEAD, self.LIVE]],
                'is_active': True,
            },
        )
        # org-2 same-named EMPTY shadow pod (tiers=[[]]).
        TeamRoster.objects.update_or_create(
            organization_id=2,
            team_name='admin-ops',
            defaults={
                'organization_slug': 'shizuha-digital',
                'tiers': [[]],
                'is_active': True,
            },
        )

    def test_empty_shadow_pod_does_not_block_org1_drain_or_emit_f3_verdict(self):
        from tasks.models import Comment, Item

        item = Item.objects.create(
            title='CTX-561 work', mode='task', status='in_progress',
            project=self.project,
            workflow_name='autonomous-bug', assignment_group='admin-ops',
            assignee_id=140, assignee_email=self.DEAD,
        )

        with mock.patch('tasks.tasks.logger') as logger:
            self._run_sweep({'admin-ops': [[self.DEAD, self.LIVE]]},
                            disabled={self.DEAD})

        # The org-1 pod drains the provider-dead owner's in_progress work to the
        # live same-team peer in one pass.
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, self.LIVE)
        self.assertEqual(
            Comment.objects.filter(item=item, author_email='system@pulse',
                                   content__startswith='Auto-failover').count(), 1)

        # The empty org-2 shadow pod must NOT emit a misleading "no live member"
        # or "no live peer" verdict — it has no down identities, so it is not a
        # stranded-work case.
        misleading = [
            c.args[0] for c in logger.warning.call_args_list + logger.info.call_args_list
            if 'no live member' in str(c.args[0]) or 'no live peer' in str(c.args[0])
        ]
        self.assertEqual(misleading, [])


class F4TickRefireTests(_SweepFixtureMixin, TestCase):
    def test_second_tick_is_a_noop(self):
        """The 17:00→17:10 BKST-1 loop: once drained to a live peer, the next
        sweep tick must neither reassign nor post another comment."""
        from tasks.models import Comment, Item

        item = Item.objects.create(
            title='BKST work', mode='task', status='open',
            project=self.project,
            workflow_name='autonomous-dev', assignment_group='books',
            assignee_id=112, assignee_email=BANTO_ALIAS,
        )
        roster = {'books': [[BANTO_ALIAS, NAO]]}

        self._run_sweep(roster, disabled={BANTO_ALIAS})
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, NAO)

        self._run_sweep(roster, disabled={BANTO_ALIAS})
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, NAO)
        self.assertEqual(
            Comment.objects.filter(item=item, author_email='system@pulse',
                                   content__startswith='Auto-failover').count(), 1)


class _FakeRedis:
    def __init__(self, hold_lock=False):
        self.store = {}
        self.hold_lock = hold_lock
        self.deleted = []

    def set(self, key, value, nx=False, ex=None):
        if self.hold_lock or (nx and key in self.store):
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)


class F2SingleFlightLockTests(TestCase):
    def test_overlapping_run_is_skipped(self):
        from tasks.tasks import sweep_rebalance_pods

        with mock.patch.object(WorkflowService, '_redis_client',
                               return_value=_FakeRedis(hold_lock=True)), \
             mock.patch('tasks.tasks._sweep_rebalance_pods_impl') as impl:
            result = sweep_rebalance_pods.apply(args=[], kwargs={}).get()

        impl.assert_not_called()
        self.assertEqual(result.get('skipped'), 'sweep_already_running')

    def test_scoped_availability_event_retries_when_global_sweep_holds_lock(self):
        from celery.exceptions import Retry
        from tasks.tasks import sweep_rebalance_pods

        with mock.patch.object(WorkflowService, '_redis_client',
                               return_value=_FakeRedis(hold_lock=True)), \
             mock.patch('tasks.tasks._sweep_rebalance_pods_impl') as impl, \
             mock.patch.object(sweep_rebalance_pods, 'retry',
                               side_effect=Retry('retry scoped reconcile')) as retry:
            with self.assertRaises(Retry):
                sweep_rebalance_pods.run(only_agent_email='sara@shizuha.com')

        retry.assert_called_once_with(countdown=5, max_retries=6)
        impl.assert_not_called()

    def test_lock_acquired_run_and_released(self):
        from tasks.tasks import (
            _REBALANCE_SWEEP_LOCK_KEY,
            _rebalance_sweep_lock_key,
            sweep_rebalance_pods,
        )

        fake = _FakeRedis()
        with mock.patch.object(WorkflowService, '_redis_client', return_value=fake), \
             mock.patch('tasks.tasks._sweep_rebalance_pods_impl',
                        return_value={'moves': 0, 'pods': {}}) as impl:
            result = sweep_rebalance_pods.apply(args=[], kwargs={}).get()

        impl.assert_called_once()
        self.assertEqual(result, {'moves': 0, 'pods': {}})
        self.assertIn(_rebalance_sweep_lock_key(), fake.deleted)
        self.assertTrue(fake.deleted[0].startswith(
            f'{_REBALANCE_SWEEP_LOCK_KEY}:test:'
        ))

    def test_redis_outage_fails_open(self):
        from tasks.tasks import sweep_rebalance_pods

        with mock.patch.object(WorkflowService, '_redis_client',
                               side_effect=Exception('redis down')), \
             mock.patch('tasks.tasks._sweep_rebalance_pods_impl',
                        return_value={'moves': 0, 'pods': {}}) as impl:
            result = sweep_rebalance_pods.apply(args=[], kwargs={}).get()

        impl.assert_called_once()
        self.assertNotIn('skipped', result)


class F5CommentDedupeTests(TestCase):
    def test_identical_system_comment_posted_once_within_window(self):
        from tasks.models import Comment, Item
        from tasks.tasks import _create_system_comment_deduped

        item = Item.objects.create(
            title='dedupe target', mode='task', status='open',
            workflow_name='autonomous-dev', assignment_group='books',
        )
        content = 'Auto-failover to **nao@shizuha.com** — primary **banto** is unavailable.'
        first = _create_system_comment_deduped(item, content)
        second = _create_system_comment_deduped(item, content)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(Comment.objects.filter(item=item, content=content).count(), 1)
        # A DIFFERENT content is a new event and must still post.
        self.assertIsNotNone(_create_system_comment_deduped(item, content + ' [x]'))


class PLS235FailoverCooldownTests(TestCase):
    def test_any_recent_auto_failover_comment_triggers_cooldown(self):
        from datetime import timedelta

        from django.utils import timezone

        from tasks.models import Comment, Item
        from tasks.tasks import _auto_failover_recently_recorded

        item = Item.objects.create(
            title='cooldown target', mode='task', status='open',
            workflow_name='autonomous-dev', assignment_group='qa',
        )
        self.assertFalse(_auto_failover_recently_recorded(item, window_hours=4))
        Comment.objects.create(
            item=item, author_id=0, author_email='system@pulse',
            content='Auto-failover to **hana@shizuha.com** — primary **banto** is unavailable.',
        )
        self.assertTrue(_auto_failover_recently_recorded(item, window_hours=4))

        # A different target/source still counts: cooldown is per item, not per
        # identical comment string, which prevents ping-pong variants.
        Comment.objects.create(
            item=item, author_id=0, author_email='system@pulse',
            content='Auto-failover to **banto@shizuha.com** — primary **hana** is unavailable.',
        )
        self.assertTrue(_auto_failover_recently_recorded(item, window_hours=4))

        Comment.objects.filter(item=item).update(created_at=timezone.now() - timedelta(hours=5))
        self.assertFalse(_auto_failover_recently_recorded(item, window_hours=4))


class CleanupCommandTests(TestCase):
    def test_collapses_consecutive_duplicate_system_comments(self):
        from django.core.management import call_command

        from tasks.models import Comment, Item

        item = Item.objects.create(
            title='spammed item', mode='task', status='open',
            workflow_name='autonomous-dev', assignment_group='books',
        )

        def _c(author, content):
            return Comment.objects.create(
                item=item, author_id=0 if author == 'system@pulse' else 7,
                author_email=author, content=content)

        _c('system@pulse', 'Auto-failover to **banto@shizuha.com** — down.')
        _c('system@pulse', 'Auto-failover to **banto@shizuha.com** — down.')
        _c('system@pulse', 'Auto-failover to **banto@shizuha.com** — down.')
        _c('nao@shizuha.com', 'real handoff note')
        _c('system@pulse', 'Auto-failover to **banto@shizuha.com** — down.')  # kept: run broken by human comment
        _c('system@pulse', 'Auto-failover to **nao@shizuha.com** — down.')
        _c('system@pulse', 'Auto-failover to **nao@shizuha.com** — down.')

        call_command('collapse_duplicate_system_comments', all=True)
        remaining = list(Comment.objects.filter(item=item)
                         .order_by('created_at').values_list('content', flat=True))
        self.assertEqual(remaining, [
            'Auto-failover to **banto@shizuha.com** — down.',
            'real handoff note',
            'Auto-failover to **banto@shizuha.com** — down.',
            'Auto-failover to **nao@shizuha.com** — down.',
        ])


class PLAT8821ParkedFailoverTests(_SweepFixtureMixin, TestCase):
    """PLAT-8821: deliberate parks never enter the failover rotation.

    CON-389 accumulated 5 auto-failover hops in one day against the operator's
    twice-stated park directive; PLAT-8787 chained 4-deep on a task whose PR
    was already merged. A parked ticket (backlog/deferred, or carrying a
    recent operator/lead park disposition) plus an offline assignee must
    produce NO failover and NO notification churn, and a candidate with no
    actionable work left (linked PR merged, none open) must not be leased.
    """

    def test_backlog_item_on_down_assignee_is_not_failed_over(self):
        from tasks.models import Comment, Item

        item = Item.objects.create(
            title='Parked ingest work', mode='task', status='backlog',
            project=self.project,
            workflow_name='autonomous-dev', assignment_group='books',
            assignee_id=112, assignee_email=BANTO_ALIAS,
        )

        self._run_sweep({'books': [[BANTO_PRIMARY, NAO]]},
                        disabled={BANTO_ALIAS})

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, BANTO_ALIAS)  # park stands
        self.assertEqual(
            Comment.objects.filter(
                item=item, author_email='system@pulse',
                content__startswith='Auto-failover',
            ).count(),
            0,
        )

    def test_operator_park_disposition_blocks_failover(self):
        from tasks.models import Comment, Item

        item = Item.objects.create(
            title='Operator-parked work', mode='task', status='open',
            project=self.project,
            workflow_name='autonomous-dev', assignment_group='books',
            assignee_id=112, assignee_email=BANTO_ALIAS,
        )
        Comment.objects.create(
            item=item, author_id=0, author_email='operator@example.test',
            content=(
                'Do not run this ticket as a weekly GLM reminder — recurrence '
                'lives in beat_schedule. Park directive; missed-period '
                'failovers were the anti-pattern.'
            ),
        )

        self._run_sweep({'books': [[BANTO_PRIMARY, NAO]]},
                        disabled={BANTO_ALIAS})

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, BANTO_ALIAS)  # disposition honored
        self.assertEqual(
            Comment.objects.filter(
                item=item, author_email='system@pulse',
                content__startswith='Auto-failover',
            ).count(),
            0,
        )

    def test_agent_prose_cannot_park_a_ticket(self):
        """Author identity matters: the same directive prose from an AGENT must
        not park the ticket — only operator/lead dispositions count."""
        from tasks.models import Comment, Item

        item = Item.objects.create(
            title='Agent-commented work', mode='task', status='open',
            project=self.project,
            workflow_name='autonomous-dev', assignment_group='books',
            assignee_id=112, assignee_email=BANTO_ALIAS,
        )
        Comment.objects.create(
            item=item, author_id=0, author_email='sora@shizuha.com',
            content='Do not run this ticket as a weekly reminder — park directive.',
        )

        self._run_sweep({'books': [[BANTO_PRIMARY, NAO]]},
                        disabled={BANTO_ALIAS})

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, NAO)  # still failed over

    def test_merged_pr_item_is_not_failed_over(self):
        """PLAT-8787 class: a candidate whose linked PR is merged with no open
        PR has no actionable work left — the item's own workflow owns the next
        step, not the failover rotation."""
        from tasks.models import Comment, Item

        item = Item.objects.create(
            title='Merged-PR work', mode='task', status='in_review',
            project=self.project,
            workflow_name='autonomous-dev', assignment_group='books',
            assignee_id=112, assignee_email=BANTO_ALIAS,
            metadata={'pull_requests': [
                {'url': 'https://origin.shizuha.com/shizuha-labs/pulse/pulls/500',
                 'status': 'merged'},
            ]},
        )

        self._run_sweep({'books': [[BANTO_PRIMARY, NAO]]},
                        disabled={BANTO_ALIAS})

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, BANTO_ALIAS)  # no lease
        self.assertEqual(
            Comment.objects.filter(
                item=item, author_email='system@pulse',
                content__startswith='Auto-failover',
            ).count(),
            0,
        )

    def test_open_pr_item_still_fails_over(self):
        """Control: an open linked PR means actionable work — failover fires."""
        from tasks.models import Item

        item = Item.objects.create(
            title='Open-PR work', mode='task', status='in_review',
            project=self.project,
            workflow_name='autonomous-dev', assignment_group='books',
            assignee_id=112, assignee_email=BANTO_ALIAS,
            metadata={'pull_requests': [
                {'url': 'https://origin.shizuha.com/shizuha-labs/pulse/pulls/501',
                 'status': 'open'},
            ]},
        )

        self._run_sweep({'books': [[BANTO_PRIMARY, NAO]]},
                        disabled={BANTO_ALIAS})

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, NAO)
