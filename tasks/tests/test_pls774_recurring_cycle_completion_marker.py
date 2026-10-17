"""PLS-774: legacy completion-marker comment parks the recurring cycle.

Production-order regressions (HLD: Recurring task cycle completion and readiness
contract, wiki 7f77f036-6f9d-4fc9-a82a-5841e5de5566):

1. A completion-marker comment dispatches a transaction-on-commit
   ``complete_cycle()`` attempt; the cycle parks in ``scheduled`` on commit.
2. The marker path never synchronously pre-empts an explicit caller's own
   completion (merged-PR / terminal-task trigger) — both paths are CAS-safe and
   the previously-blocked PLAT-1353 caller still closes exactly once.
3. Parked (``scheduled``) recurring items are immune to rebalance/recovery
   sweeps: repeated passes change no status/owner/lease.
4. The exact due receipt opens the next generation exactly once; a duplicate
   receipt is a no-op.
5. Concurrent/repeated marker deliveries converge to one completion watermark
   and one ``scheduled`` state (idempotent under replay).
"""

import time
from datetime import timedelta
from unittest import mock

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from tasks.models import Comment, Item, Project, Status, TeamRoster
from tasks.services.recurring_cycle_close import (
    alert_recurring_cycle_auto_close_failure,
    close_recurring_cycle_if_complete,
)
from tasks.services.recurring_cycle_state import (
    complete_cycle,
    cycle_snapshot,
    fire_due_cycle,
)
from tasks.services.workflow_service import WorkflowService

MARKER = "cycle 2026-07-04 complete — next due 2026-07-11"


def _ensure_scheduled_status():
    Status.objects.get_or_create(
        slug='scheduled',
        defaults={
            'name': 'Scheduled',
            'category': 'scheduled',
            'color': '#64748b',
            'position': 99,
        },
    )


def _recurring_item(project, **kw):
    kw.setdefault('title', 'weekly durability audit')
    kw.setdefault('mode', 'task')
    kw.setdefault('status', 'open')
    kw.setdefault('workflow_name', 'autonomous-dev')
    kw.setdefault('workflow_status', 'Open')
    kw.setdefault('is_recurring', True)
    kw.setdefault('frequency', 'weekly')
    kw.setdefault('due_date', timezone.now() - timedelta(hours=1))
    return Item.objects.create(project=project, **kw)


class CompletionMarkerOnCommitTests(TransactionTestCase):
    """The marker path parks the cycle on the comment transaction's commit.

    A real TransactionTestCase commit is what fires ``transaction.on_commit`` in
    the signal — mirroring production, where the comment save commits before the
    deferred completion attempt runs.
    """

    def setUp(self):
        _ensure_scheduled_status()
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1, access_level='open',
        )
        TeamRoster.objects.update_or_create(
            team_name='accounting',
            organization_slug='shizuha',
            defaults={
                'organization_id': 1,
                'tiers': [['banto@shizuha.com']],
                'is_active': True,
            },
        )

    def test_marker_comment_parks_cycle_on_commit(self):
        item = _recurring_item(self.project)
        old_due = item.due_date

        Comment.objects.create(
            item=item,
            author_id=0,
            author_email='mika@shizuha.com',
            content=MARKER,
        )

        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')
        self.assertGreater(item.due_date, old_due)
        self.assertFalse(item.overdue_notified)
        self.assertFalse(item.reminder_sent)
        cycle = item.metadata['recurring_cycle']
        self.assertEqual(cycle['consecutive_skips'], 0)
        self.assertEqual(cycle['completed_generation'], cycle['active_generation'])
        # The deterministic event key names the comment; a replay is a no-op.
        self.assertTrue(
            any(
                t.startswith('comment:')
                for t in (cycle.get('auto_closed_triggers') or [])
            ),
            cycle.get('auto_closed_triggers'),
        )

    def test_repeated_marker_comments_converge_to_one_scheduled_state(self):
        item = _recurring_item(self.project)

        Comment.objects.create(item=item, author_id=0, author_email='mika@shizuha.com', content=MARKER)
        first = cycle_snapshot(Item.objects.get(pk=item.pk))
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')

        # A second identical marker after the park must stay a no-op: no extra
        # generation, no re-open, no second watermark.
        Comment.objects.create(item=item, author_id=0, author_email='mika@shizuha.com', content=MARKER)
        item.refresh_from_db()
        second = cycle_snapshot(item)
        self.assertEqual(item.status, 'scheduled')
        self.assertEqual(second['generation'], first['generation'])
        self.assertEqual(
            second['completion_watermark'],
            first['completion_watermark'],
        )

    def test_marker_before_and_after_stop_progress_both_park(self):
        # Marker AFTER Stop Progress: item back to 'open', marker parks it.
        item = _recurring_item(self.project)
        item.status = 'open'
        item.workflow_status = 'Open'
        item.save(update_fields=['status', 'workflow_status', 'updated_at'])
        Comment.objects.create(
            item=item, author_id=0, author_email='mika@shizuha.com', content=MARKER,
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')

        # Marker BEFORE Stop Progress: still parked after a later Stop Progress.
        item2 = _recurring_item(self.project, title='marker-before-stop')
        Comment.objects.create(
            item=item2, author_id=0, author_email='mika@shizuha.com', content=MARKER,
        )
        item2.refresh_from_db()
        self.assertEqual(item2.status, 'scheduled')
        # A "stop progress" flavour write on a parked item must not re-open it.
        item2.status = 'open'
        item2.workflow_status = 'Open'
        item2.save(update_fields=['status', 'workflow_status', 'updated_at'])
        item2.refresh_from_db()
        cycle = item2.metadata['recurring_cycle']
        self.assertGreaterEqual(cycle['completed_generation'], cycle['active_generation'])
        # Re-parking stays idempotent and cheap.
        Comment.objects.create(
            item=item2, author_id=0, author_email='mika@shizuha.com', content=MARKER,
        )
        item2.refresh_from_db()
        self.assertEqual(item2.status, 'scheduled')

    def test_audit_marker_comment_does_not_loop(self):
        item = _recurring_item(self.project)
        Comment.objects.create(
            item=item,
            author_id=0,
            author_email='system@pulse',
            content='[recurring-cycle auto-close] Closed current cycle after linked PR `#1` completed.',
        )
        item.refresh_from_db()
        # The audit marker own-comment must NOT self-trigger another completion
        # (no infinite loop); the item simply isn't parked by it.
        self.assertEqual(item.status, 'open')

    def test_non_completion_comment_does_not_park(self):
        item = _recurring_item(self.project, title='ordinary comment')
        Comment.objects.create(
            item=item, author_id=0, author_email='mika@shizuha.com', content='just checking in',
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'open')

    def test_mechanism_failure_is_fail_loud_on_the_item(self):
        item = _recurring_item(self.project)
        with mock.patch(
            'tasks.services.recurring_cycle_state.complete_cycle',
            side_effect=RuntimeError('cycle store unavailable'),
        ), mock.patch(
            'tasks.services.recurring_cycle_close.alert_recurring_cycle_auto_close_failure',
            wraps=alert_recurring_cycle_auto_close_failure,
        ) as loud:
            with self.assertLogs('tasks.signals', level='WARNING'):
                Comment.objects.create(
                    item=item, author_id=0, author_email='mika@shizuha.com', content=MARKER,
                )
            loud.assert_called_once()

    def test_due_receipt_opened_exactly_once(self):
        item = _recurring_item(
            self.project,
            assignment_group='accounting',
            assignee_id=112,
            assignee_email='banto@shizuha.com',
        )
        Comment.objects.create(
            item=item, author_id=0, author_email='mika@shizuha.com', content=MARKER,
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')
        # Bring the parked item to/at its due so the fire opener accepts it.
        item.due_date = timezone.now() - timedelta(minutes=1)
        item.save(update_fields=['due_date', 'updated_at'])
        snap = cycle_snapshot(item)

        opened = fire_due_cycle(
            item.pk,
            expected_cycle_revision=snap['cycle_revision'],
            expected_due_at=snap['due_boundary'],
            expected_generation=snap['generation'],
            expected_completion_watermark=snap['completion_watermark'] or '',
        )
        item.refresh_from_db()
        self.assertTrue(opened.applied)
        self.assertEqual(opened.kind, 'scheduled_start')
        self.assertNotEqual(item.status, 'scheduled')
        gen_after_open = item.metadata['recurring_cycle']['active_generation']

        # Duplicate receipt with the same exact expected state is a stale no-op.
        replay = fire_due_cycle(
            item.pk,
            expected_cycle_revision=snap['cycle_revision'],
            expected_due_at=snap['due_boundary'],
            expected_generation=snap['generation'],
            expected_completion_watermark=snap['completion_watermark'] or '',
        )
        item.refresh_from_db()
        self.assertFalse(replay.applied)
        self.assertEqual(
            item.metadata['recurring_cycle']['active_generation'],
            gen_after_open,
        )


class RebalanceImmunityTests(TransactionTestCase):
    """Parked recurring items survive rebalance/recovery sweeps unchanged.

    ``TransactionTestCase`` so the marker's ``transaction.on_commit`` fires (a
    real commit) — the item is actually parked, then swept, in production order.
    """

    def setUp(self):
        _ensure_scheduled_status()
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1, access_level='open',
        )
        TeamRoster.objects.create(
            organization_id=1,
            organization_slug='shizuha',
            team_name='accounting',
            tiers=[['banto@shizuha.com']],
            wip_enforced=True,
            ready_wip_cap=5,
            active_wip_cap=10,
            is_active=True,
        )

    def test_marker_parked_item_is_not_a_rebalance_candidate(self):
        from tasks.tasks import _sweep_rebalance_pods_impl

        item = _recurring_item(
            self.project,
            assignment_group='accounting',
            assignee_id=112,
            assignee_email='banto@shizuha.com',
        )
        Comment.objects.create(
            item=item,
            author_id=0,
            author_email='mika@shizuha.com',
            content=MARKER,
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')

        def _user(email):
            return {'id': 112, 'email': email}

        def _sweep():
            import os
            with mock.patch(
                'tasks.tasks._rebalance_pulse_api_health_gate',
                return_value=(True, ''),
            ), mock.patch.dict(
                os.environ,
                {'PULSE_HEALTH_EXPORTER_URL': ''},
                clear=False,
            ), mock.patch.object(
                WorkflowService,
                '_pod_roster',
                return_value={'accounting': ['banto@shizuha.com']},
            ), mock.patch.object(
                WorkflowService,
                '_is_agent_available_for_assignment',
                return_value=True,
            ), mock.patch.object(
                WorkflowService,
                '_member_can_execute_from_status',
                return_value=True,
            ), mock.patch.object(
                WorkflowService,
                '_is_human_account',
                return_value=False,
            ), mock.patch.object(
                WorkflowService,
                '_disabled_agent_emails',
                return_value=set(),
            ), mock.patch.object(
                WorkflowService,
                '_cap_applies',
                return_value=False,
            ), mock.patch(
                'tasks.user_service.user_service.get_user_by_email',
                side_effect=_user,
            ):
                return _sweep_rebalance_pods_impl(max_skew=0)

        pre = (
            item.assignee_email,
            item.status,
            item.updated_at,
        )
        for _ in range(2):
            _sweep()
            # The parked recurring item stays out of the movable/failover view:
            # it is not reassigned, re-opened, or touched.
            item.refresh_from_db()
            self.assertEqual(item.status, 'scheduled')
            self.assertEqual(item.assignee_email, 'banto@shizuha.com')
        post = (
            item.assignee_email,
            item.status,
            item.updated_at,
        )
        self.assertEqual(pre, post)


class ExplicitCallerCoexistenceTests(TestCase):
    """The marker path must not break the pre-existing explicit caller path.

    Mirrors the original PLAT-1353 caller (plain ``TestCase``: the deferred
    on-commit marker is not executed mid-test, so the explicit merged-PR caller
    owns the close once) — this is the exact sequence that was red on the
    pre-fix PR head and must stay green.
    """

    def setUp(self):
        _ensure_scheduled_status()
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1, access_level='open',
        )

    def test_explicit_close_still_owns_when_marker_present(self):
        from tasks.services.recurring_cycle_close import close_recurring_cycle_if_complete
        item = _recurring_item(self.project, metadata={
            'pull_requests': [
                {
                    'url': 'https://github.com/shizuha-labs/deploy/pull/280',
                    'repo': 'shizuha-labs/deploy',
                    'number': 280,
                    'status': 'merged',
                },
            ],
        })
        old_due = item.due_date

        # Strong completeness marker posted by the PR author.
        Comment.objects.create(
            item=item,
            author_id=1,
            author_email='kai@shizuha.com',
            content='cycle 2026-07-04 complete — deliverable merged and verified',
        )

        result = close_recurring_cycle_if_complete(
            item,
            trigger_kind='PR',
            trigger_label='https://github.com/shizuha-labs/deploy/pull/280',
            actor_email='github-webhook@shizuha.local',
        )

        self.assertTrue(result.closed)
        item.refresh_from_db()
        self.assertEqual(item.status, 'scheduled')
        self.assertGreater(item.due_date, old_due)
        self.assertFalse(item.overdue_notified)
        self.assertFalse(item.reminder_sent)
        cycle = item.metadata['recurring_cycle']
        self.assertEqual(cycle['consecutive_skips'], 0)
        self.assertEqual(
            cycle['last_auto_closed_trigger'],
            'PR:https://github.com/shizuha-labs/deploy/pull/280',
        )
        self.assertTrue(
            Comment.objects.filter(
                item=item,
                content__contains='[recurring-cycle auto-close]',
            ).exists(),
        )

        duplicate = close_recurring_cycle_if_complete(
            item,
            trigger_kind='PR',
            trigger_label='https://github.com/shizuha-labs/deploy/pull/280',
            actor_email='github-webhook@shizuha.local',
        )
        self.assertFalse(duplicate.closed)
        self.assertEqual(
            Comment.objects.filter(
                item=item,
                content__contains='[recurring-cycle auto-close]',
            ).count(),
            1,
        )
