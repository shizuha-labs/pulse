"""PLAT-390 (impl of PLAT-388 option-2): two-tier engineering WIP cap.

Decouples eng throughput from review capacity. A member is "full" for NEW work
iff hands-on (active = open+in_progress, excluding the review pipeline) reaches
ACTIVE_CAP, OR total in-flight (active + in_review/in_qa/awaiting_merge/applied)
reaches INFLIGHT_CAP. Fixes the PLAT-307/PLAT-375 starvation where an engineer
with submissions sitting in the thin review pipeline (e.g. 0 in_progress +
4 in_review) read as "full" and never got pulled queued work.

Also covers PLAT-534: engineering capacity-pull must gate on team AND status-role
(never select awaiting_deploy/in_review/awaiting_merge/verification tasks).

Pure unit tests: the two counts and DB-touching deps are patched, so no database
or running stack is required. DB-backed tests use Django TestCase where needed.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier, Event, Thread
from unittest.mock import patch

from django.db import IntegrityError, close_old_connections, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from tasks.services.workflow_service import WorkflowService
from tasks.tasks import _reclaim_wip_overload, sweep_rebalance_pods
from .base import create_blocked_item, satisfy_blocked_invariant


def _set_shizuha_rosters(rosters):
    """Persist the org-scoped roster source used by DB-backed sweep tests."""
    from tasks.models import TeamRoster

    for team_name, members in rosters.items():
        tiers = members if members and isinstance(members[0], list) else [members]
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name=team_name,
            defaults={
                'organization_slug': 'shizuha',
                'tiers': tiers,
                'is_active': True,
            },
        )


class TwoTierPredicateTests(SimpleTestCase):
    """The core two-count predicate `_at_capacity(active, inflight)`."""

    def test_default_caps(self):
        # Active concurrency is uncapped by default; the legacy total in-flight
        # runaway bound remains. This asserts the SHIPPED default;
        # the behavioral predicate tests below pin caps explicitly so they stay
        # hermetic against future default changes.
        self.assertEqual(WorkflowService._wip_cap(), 0)        # ACTIVE_CAP disabled
        self.assertEqual(WorkflowService._inflight_cap(), 15)  # INFLIGHT_CAP

    def test_sara_case_review_pipeline_is_eligible(self):
        # 0 in_progress + 4 in_review → active=0, inflight=4 → NOT full (the fix).
        self.assertFalse(WorkflowService._at_capacity(0, 4))

    @patch.dict(os.environ, {'PULSE_WIP_CAP': '5', 'PULSE_INFLIGHT_CAP': '10'})
    def test_active_cap_makes_full(self):
        # 5 hands-on tasks → at ACTIVE_CAP → full regardless of inflight.
        self.assertTrue(WorkflowService._at_capacity(5, 5))
        self.assertTrue(WorkflowService._at_capacity(6, 6))

    @patch.dict(os.environ, {'PULSE_WIP_CAP': '5', 'PULSE_INFLIGHT_CAP': '10'})
    def test_inflight_runaway_bound_holds(self):
        # active 4 (< 5) but total in-flight 10 → INFLIGHT_CAP → full (runaway bound).
        self.assertTrue(WorkflowService._at_capacity(4, 10))

    @patch.dict(os.environ, {'PULSE_WIP_CAP': '5', 'PULSE_INFLIGHT_CAP': '10'})
    def test_under_both_caps_eligible(self):
        # active 4 (< 5) and inflight 9 (< 10) → eligible.
        self.assertFalse(WorkflowService._at_capacity(4, 9))

    @patch.dict(os.environ, {'PULSE_WIP_CAP': '3', 'PULSE_INFLIGHT_CAP': '6'})
    def test_caps_are_configurable(self):
        self.assertEqual(WorkflowService._wip_cap(), 3)
        self.assertEqual(WorkflowService._inflight_cap(), 6)
        self.assertFalse(WorkflowService._at_capacity(2, 5))
        self.assertTrue(WorkflowService._at_capacity(3, 5))   # active cap=3
        self.assertTrue(WorkflowService._at_capacity(2, 6))   # inflight cap=6

    @patch.dict(os.environ, {'PULSE_WIP_CAP': '5', 'PULSE_INFLIGHT_CAP': '0'})
    def test_inflight_cap_zero_disables_that_tier(self):
        # <=0 disables the in-flight tier; only the active cap gates.
        self.assertFalse(WorkflowService._at_capacity(4, 99))
        self.assertTrue(WorkflowService._at_capacity(5, 99))

    @patch.dict(os.environ, {'PULSE_WIP_CAP': '0', 'PULSE_INFLIGHT_CAP': '10'})
    def test_active_cap_zero_keeps_inflight_tier_enabled(self):
        # ACTIVE_CAP can be disabled without disabling the runaway bound.
        self.assertFalse(WorkflowService._at_capacity(99, 9))
        self.assertTrue(WorkflowService._at_capacity(0, 10))
        self.assertTrue(WorkflowService._cap_applies('engineering'))

    def test_qa_capacity_queue_is_cap_enforced_by_default(self):
        # PLAT-622: QA cadence work can be group-queued, so the sweep must drain
        # QA's capacity queue to an under-cap active reviewer just like engineering.
        self.assertIn('qa', WorkflowService._cap_enforced_teams())
        self.assertTrue(WorkflowService._cap_applies('qa'))

    def test_review_team_remains_non_cap_by_default(self):
        # Review routing is pipeline-load based; PLAT-622 only adds QA to the
        # default cap-enforced teams, not every review-like pod.
        self.assertNotIn('review', WorkflowService._cap_enforced_teams())
        self.assertFalse(WorkflowService._cap_applies('review'))

    @patch.dict(os.environ, {'PULSE_WIP_CAP': '0', 'PULSE_INFLIGHT_CAP': '0'})
    def test_both_caps_zero_disables_cap_pull(self):
        # Both tiers disabled → _cap_applies False → capacity-queue pull event
        # hook bails without touching the DB (teams list is empty).
        self.assertFalse(WorkflowService._cap_applies('engineering'))
        self.assertIsNone(WorkflowService._pull_next_queued_for(4, 'sara@shizuha.com'))


class AtomicWipAdmissionTests(TransactionTestCase):
    """PLS-509: reservation closes resolver→save races and recovery is lossless."""

    EMAIL = 'wip-seat@example.com'
    UID = 9509

    def setUp(self):
        from tasks.models import Project, TeamRoster
        self.project = Project.objects.create(
            org_id=1,
            name='Atomic WIP Admission',
            key='AWIP',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='review',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.EMAIL]], 'junior_emails': [self.EMAIL], 'is_active': True,
                'wip_enforced': True, 'ready_wip_cap': 1,
                'active_wip_cap': 1,
            },
        )

    def _user_service(self):
        target_email = self.EMAIL
        uid = self.UID

        class FakeUserService:
            def get_user_by_email(self, value):
                return {
                    'id': uid,
                    'email': target_email,
                } if value == target_email else None

            def get_user_by_id(self, value):
                return {
                    'id': uid,
                    'email': target_email,
                } if int(value) == uid else None

            def resolve_user_info(self, user_id=None, email=None):
                if user_id is not None and int(user_id) != uid:
                    return None
                if email and email.strip().lower() != target_email:
                    return None
                return {
                    'id': uid,
                    'email': target_email,
                    'username': 'wip-seat',
                    'full_name': 'WIP Seat',
                    'is_agent': True,
                }
        return FakeUserService()

    def test_pending_reservation_prevents_second_ready_assignment(self):
        from tasks.models import Item, WipSlotReservation
        first = Item.objects.create(
            title='first ready', mode='task', status='open', priority='normal',
            assignment_group='review', workflow_name='autonomous-dev', project=self.project,
        )
        second = Item.objects.create(
            title='second ready', mode='task', status='open', priority='high',
            assignment_group='review', workflow_name='autonomous-dev', project=self.project,
        )
        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            self.assertEqual(
                WorkflowService._resolve_team_assignee('review', item=first),
                (self.UID, self.EMAIL),
            )
            self.assertEqual(
                WorkflowService._resolve_team_assignee('review', item=second),
                (None, None),
            )
            first.assign({'id': self.UID, 'email': self.EMAIL}, save=True)
            self.assertFalse(WipSlotReservation.objects.exists())
            self.assertEqual(
                WorkflowService._resolve_team_assignee('review', item=second),
                (None, None),
            )

    def test_agent_alias_admissions_share_one_cap_one_lock_row(self):
        """Primary and agent-domain requests cannot reserve separate seats."""
        from tasks.models import AgentWipCapacity, Item, WipSlotReservation

        primary = 'wip-alias@shizuha.com'
        alias = 'wip-alias@agents.shizuha.io'
        first = Item.objects.create(
            title='primary alias admission', mode='task', status='open',
            priority='high', assignment_group='review',
            workflow_name='autonomous-dev', project=self.project,
        )
        second = Item.objects.create(
            title='agent alias admission', mode='task', status='open',
            priority='high', assignment_group='review',
            workflow_name='autonomous-dev', project=self.project,
        )

        barrier = Barrier(2)

        def reserve(candidate, candidate_email):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return WorkflowService._reserve_wip_slot(
                    candidate, self.UID, candidate_email,
                )
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda pair: reserve(*pair),
                ((first, primary), (second, alias)),
            ))

        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(AgentWipCapacity.objects.count(), 1)
        capacity = AgentWipCapacity.objects.get()
        self.assertEqual(capacity.assignee_email, primary)
        self.assertEqual(capacity.assignee_id, self.UID)
        self.assertEqual(WipSlotReservation.objects.count(), 1)
        denied = first if results[0] is False else second
        self.assertEqual(
            denied._wip_capacity_diagnostics['reservation_owner_kind'],
            'pending_reservation',
        )

    def test_alias_reservation_survives_directory_email_canonicalization(self):
        """Reserve by agent alias, then consume after ID resolves primary email."""
        from tasks.models import Item, WipSlotReservation

        primary = 'wip-canonical@shizuha.com'
        alias = 'wip-canonical@agents.shizuha.io'
        item = Item.objects.create(
            title='alias canonical consumption', mode='task', status='open',
            priority='urgent', assignment_group='review',
            workflow_name='autonomous-dev', project=self.project,
        )
        self.assertTrue(
            WorkflowService._reserve_wip_slot(item, self.UID, alias)
        )

        item.assignee_id = self.UID
        item.assignee_email = primary
        with patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ), patch.object(
            Item,
            '_resolve_user_ref',
            return_value=(self.UID, primary),
        ):
            item.save(update_fields=['assignee_id', 'assignee_email', 'updated_at'])

        self.assertFalse(WipSlotReservation.objects.exists())
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, primary)

    def test_consumption_does_not_hold_capacity_mutex_through_outer_transaction(self):
        """PLS-912: Item/post-function work cannot pin the mint mutex.

        Production order:
          1. one request mints a positive-cap reservation;
          2. its Item save consumes that reservation inside a still-open outer
             transition transaction (standing in for later post-functions);
          3. a concurrent group-queue/Start contender reaches the SAME capacity
             row and receives an ordinary denial from the committed pending
             reservation instead of waiting for the outer transaction.

        The reservation row remains MVCC-visible until the holder commits, so
        removing the second capacity-row lock cannot double-admit.
        """
        from django.db import connection
        from tasks.models import Item, WipSlotReservation

        if connection.vendor != 'postgresql':
            self.skipTest('row-lock duration regression requires PostgreSQL')

        holder = Item.objects.create(
            title='capacity mutex holder', mode='task', status='open',
            priority='high', assignment_group='review',
            workflow_name='autonomous-dev', project=self.project,
        )
        contender = Item.objects.create(
            title='capacity mutex contender', mode='task', status='open',
            priority='high', assignment_group='review',
            workflow_name='autonomous-dev', project=self.project,
        )
        self.assertTrue(WorkflowService._reserve_wip_slot(
            holder, self.UID, self.EMAIL,
        ))
        token = holder._wip_reservation_token

        holder_saved = Event()
        release_holder = Event()
        holder_errors = Queue()
        contender_results = Queue()

        def hold_outer_transaction_open():
            close_old_connections()
            try:
                local = Item.objects.get(pk=holder.pk)
                local._wip_reservation_token = token
                local.assignee_id = self.UID
                local.assignee_email = self.EMAIL
                with transaction.atomic():
                    local.save(update_fields=[
                        'assignee_id', 'assignee_email', 'updated_at',
                    ])
                    holder_saved.set()
                    if not release_holder.wait(timeout=10):
                        raise TimeoutError('test did not release holder transaction')
            except BaseException as exc:  # surfaced in the assertion thread
                holder_errors.put(exc)
                holder_saved.set()
            finally:
                close_old_connections()

        def reserve_concurrently():
            close_old_connections()
            try:
                local = Item.objects.get(pk=contender.pk)
                contender_results.put(WorkflowService._reserve_wip_slot(
                    local, self.UID, self.EMAIL,
                ))
            except BaseException as exc:  # surfaced in the assertion thread
                contender_results.put(exc)
            finally:
                close_old_connections()

        with patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ), patch.object(
            Item,
            '_resolve_user_ref',
            return_value=(self.UID, self.EMAIL),
        ):
            holder_thread = Thread(target=hold_outer_transaction_open, daemon=True)
            holder_thread.start()
            self.assertTrue(holder_saved.wait(timeout=5))
            if not holder_errors.empty():
                raise holder_errors.get()

            contender_thread = Thread(target=reserve_concurrently, daemon=True)
            contender_thread.start()
            contender_thread.join(timeout=5)
            try:
                self.assertFalse(
                    contender_thread.is_alive(),
                    'concurrent capacity mint waited on the holder outer transaction',
                )
                result = contender_results.get_nowait()
                if isinstance(result, BaseException):
                    raise result
                self.assertFalse(result)
                self.assertEqual(
                    WipSlotReservation.objects.filter(token=token).count(),
                    1,
                    'uncommitted consumption must remain visible as pending admission',
                )
            finally:
                release_holder.set()
                holder_thread.join(timeout=5)

        self.assertFalse(holder_thread.is_alive())
        if not holder_errors.empty():
            raise holder_errors.get()
        self.assertFalse(WipSlotReservation.objects.exists())

    def test_zero_active_cap_allows_multiple_active_assignments(self):
        """Zero disables the review-pipeline limiting dimension (PLAT-390).

        Review-pipeline statuses (in_review) consume the READY capacity
        dimension, so uncapping ready_wip_cap must allow multiple concurrent
        reviews on the same seat even while the active cap stays enforced.
        """
        from tasks.models import Item, TeamRoster, WipSlotReservation

        TeamRoster.objects.filter(
            organization_slug='shizuha', team_name='review',
        ).update(ready_wip_cap=0)
        WorkflowService._invalidate_team_roster_caches()
        active_items = [
            Item.objects.create(
                title=f'uncapped active {idx}', mode='task', status='in_review',
                priority='urgent', assignment_group='review',
                workflow_name='autonomous-dev', project=self.project,
            )
            for idx in range(2)
        ]

        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            for item in active_items:
                self.assertEqual(
                    WorkflowService._resolve_team_assignee('review', item=item),
                    (self.UID, self.EMAIL),
                )
                self.assertFalse(hasattr(item, '_wip_reservation_token'))
                item.assign({'id': self.UID, 'email': self.EMAIL}, save=True)

        self.assertEqual(
            Item.objects.filter(
                pk__in=[item.pk for item in active_items],
                assignee_email=self.EMAIL,
            ).count(),
            2,
        )
        self.assertFalse(WipSlotReservation.objects.filter(slot_kind='active').exists())

    def test_positive_cap_denial_returns_typed_locked_snapshot(self):
        """Denial names the exact capacity generation and consuming owner."""
        from tasks import liveness
        from tasks.models import Item

        owner = Item.objects.create(
            title='current active owner', mode='task', status='in_review',
            priority='high', assignment_group='review',
            assignee_id=self.UID, assignee_email=self.EMAIL,
            workflow_name='autonomous-dev', project=self.project,
        )
        candidate = Item.objects.create(
            title='denied active candidate', mode='task', status='open',
            priority='high', assignment_group='review',
            assignee_id=self.UID, assignee_email=self.EMAIL,
            workflow_name='autonomous-dev', project=self.project,
        )

        with patch('tasks.liveness.record_heartbeat') as heartbeat:
            admitted = WorkflowService._reserve_wip_slot(
                candidate,
                self.UID,
                self.EMAIL,
                status_override='in_review',
            )

        self.assertFalse(admitted)
        # PLAT-390 two-tier: review-pipeline admission consumes the READY
        # capacity dimension (in-flight, not hands-on). The owner's in_review
        # item fills the ready cap (1/1) and denies the second review.
        self.assertEqual(candidate._wip_capacity_diagnostics, {
            'actual': 1,
            'pending': 0,
            'cap': 1,
            'reservation_owner': owner.item_key or str(owner.pk),
            'reservation_owner_kind': 'actual_item',
            'capacity_generation': 0,
            'slot_kind': 'ready',
            'assignment_group': 'review',
            'assignee_email': self.EMAIL,
            'requested_item': candidate.item_key or str(candidate.pk),
        })
        heartbeat.assert_called_once_with(liveness.WIP_ADMISSION)

    def test_urgent_assignment_ignores_a_full_wip_cap(self):
        """Operator 2026-09-27: urgent assignment seats a full WIP roster.

        Production order is the group-queue resolver: ``_resolve_team_assignee``
        reserves through ``_reserve_wip_slot`` and then ``Item.assign``. A high
        priority sibling on the same full seat stays group-queued.
        """
        from tasks.models import Item

        holding = Item.objects.create(
            title='seat already full', mode='task', status='open',
            priority='normal', assignment_group='review',
            workflow_name='autonomous-dev', project=self.project,
        )
        urgent = Item.objects.create(
            title='urgent past cap', mode='task', status='open',
            priority='urgent', assignment_group='review',
            workflow_name='autonomous-dev', project=self.project,
        )
        ordinary = Item.objects.create(
            title='high stays queued', mode='task', status='open',
            priority='high', assignment_group='review',
            workflow_name='autonomous-dev', project=self.project,
        )

        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            self.assertEqual(
                WorkflowService._resolve_team_assignee('review', item=holding),
                (self.UID, self.EMAIL),
            )
            holding.assign({'id': self.UID, 'email': self.EMAIL}, save=True)
            self.assertEqual(
                WorkflowService._resolve_team_assignee('review', item=urgent),
                (self.UID, self.EMAIL),
            )
            urgent.assign({'id': self.UID, 'email': self.EMAIL}, save=True)
            self.assertEqual(
                WorkflowService._resolve_team_assignee('review', item=ordinary),
                (None, None),
            )

        urgent.refresh_from_db()
        ordinary.refresh_from_db()
        self.assertEqual(urgent.assignee_email, self.EMAIL)
        self.assertFalse(ordinary.assignee_email)

    def test_denied_candidate_releases_savepoint_lock_before_resolver_scans_next(self):
        """Two production-order resolver scans cannot deadlock on denied seats.

        Production order from PLS-912: Start Progress / group-queue assignment
        runs inside an outer item transaction, probes one full WIP seat, then
        scans the next candidate.  A concurrent resolver scans the same two
        seats in the opposite order.  Capacity denial must roll back its nested
        savepoint so neither outer transaction carries the rejected row lock
        into the next probe.
        """
        from django.db import connection
        from tasks.models import AgentWipCapacity, Item, TeamRoster, WipSlotReservation

        second_email = 'wip-seat-two@example.com'
        second_uid = self.UID + 1
        TeamRoster.objects.filter(
            organization_slug='shizuha', team_name='review',
        ).update(
            tiers=[[self.EMAIL, second_email]],
            junior_emails=[self.EMAIL, second_email],
            ready_wip_cap=1,
        )
        WorkflowService._invalidate_team_roster_caches()

        for uid, email in ((self.UID, self.EMAIL), (second_uid, second_email)):
            AgentWipCapacity.objects.create(
                organization_slug='shizuha',
                assignee_id=uid,
                assignee_email=email,
            )
            Item.objects.create(
                title=f'active capacity owner {email}',
                mode='task',
                status='in_review',
                priority='urgent',
                assignment_group='review',
                assignee_id=uid,
                assignee_email=email,
                workflow_name='autonomous-dev',
                project=self.project,
            )

        contenders = [
            Item.objects.create(
                title=f'capacity contender {idx}',
                mode='task',
                status='open',
                priority='high',
                assignment_group='review',
                workflow_name='autonomous-dev',
                project=self.project,
            )
            for idx in range(2)
        ]
        barrier = Barrier(2)

        def scan(item, ordered_seats):
            close_old_connections()
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL statement_timeout = '5s'")
                    first = WorkflowService._reserve_wip_slot(
                        item,
                        ordered_seats[0][0],
                        ordered_seats[0][1],
                        status_override='in_review',
                    )
                    barrier.wait(timeout=10)
                    second = WorkflowService._reserve_wip_slot(
                        item,
                        ordered_seats[1][0],
                        ordered_seats[1][1],
                        status_override='in_review',
                    )
                    return first, second
            finally:
                close_old_connections()

        seat_a = (self.UID, self.EMAIL)
        seat_b = (second_uid, second_email)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(scan, contenders[0], (seat_a, seat_b)),
                pool.submit(scan, contenders[1], (seat_b, seat_a)),
            ]
            results = [future.result(timeout=15) for future in futures]

        self.assertEqual(results, [(False, False), (False, False)])
        self.assertFalse(WipSlotReservation.objects.exists())
        self.assertSetEqual(
            set(AgentWipCapacity.objects.values_list('generation', flat=True)),
            {0},
        )

    def test_capacity_lock_failure_emits_stable_metric_class(self):
        """Database lock failures remain visible with a queryable class."""
        from django.db import OperationalError
        from tasks.models import Item

        candidate = Item.objects.create(
            title='capacity lock failure candidate',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='review',
            workflow_name='autonomous-dev',
            project=self.project,
        )

        with patch(
            'django.db.transaction.atomic',
            side_effect=OperationalError('deadlock detected'),
        ), self.assertLogs(
            'tasks.services.workflow_service',
            level='ERROR',
        ) as captured:
            with self.assertRaises(OperationalError):
                WorkflowService._reserve_wip_slot(
                    candidate,
                    self.UID,
                    self.EMAIL,
                    status_override='in_review',
                )

        self.assertIn(
            'metric:wip_capacity_lock_failure class=deadlock_detected',
            '\n'.join(captured.output),
        )

    def test_foreign_org_item_neither_consumes_cap_nor_leaks_owner_key(self):
        """The org-keyed capacity row may account only for its own tenant."""
        from tasks.models import Item, Project, TeamRoster

        foreign_project = Project.objects.create(
            org_id=2,
            name='Foreign WIP Admission',
            key='FWIP',
        )
        TeamRoster.objects.update_or_create(
            organization_id=2,
            team_name='review',
            defaults={
                'organization_slug': 'shizuha-digital',
                'tiers': [[self.EMAIL]],
                'junior_emails': [self.EMAIL],
                'is_active': True,
                'wip_enforced': True,
                'ready_wip_cap': 1,
                'active_wip_cap': 1,
            },
        )
        foreign_owner = Item.objects.create(
            title='foreign active owner', mode='task', status='in_review',
            priority='urgent', assignment_group='review',
            assignee_id=self.UID, assignee_email=self.EMAIL,
            workflow_name='autonomous-dev', project=foreign_project,
        )
        local_owner = Item.objects.create(
            title='local active owner', mode='task', status='in_review',
            priority='normal', assignment_group='review',
            assignee_id=self.UID, assignee_email=self.EMAIL,
            workflow_name='autonomous-dev', project=self.project,
        )
        candidate = Item.objects.create(
            title='local denied candidate', mode='task', status='open',
            priority='high', assignment_group='review',
            assignee_id=self.UID, assignee_email=self.EMAIL,
            workflow_name='autonomous-dev', project=self.project,
        )

        admitted = WorkflowService._reserve_wip_slot(
            candidate,
            self.UID,
            self.EMAIL,
            status_override='in_review',
        )

        self.assertFalse(admitted)
        diagnostics = candidate._wip_capacity_diagnostics
        self.assertEqual(diagnostics['actual'], 1)
        self.assertEqual(
            diagnostics['reservation_owner'],
            local_owner.item_key or str(local_owner.pk),
        )
        self.assertNotEqual(
            diagnostics['reservation_owner'],
            foreign_owner.item_key or str(foreign_owner.pk),
        )

    def test_idle_synthetic_probe_traverses_and_releases_authoritative_slot(self):
        """Scheduled liveness is independent of real assignment traffic."""
        from tasks import liveness
        from tasks.models import WipSlotReservation
        from tasks.tasks import _probe_wip_admission_health_once

        with patch('tasks.liveness.record_heartbeat') as heartbeat:
            result = _probe_wip_admission_health_once()

        self.assertEqual(result['status'], 'admitted')
        self.assertTrue(result['reservation_released'])
        self.assertFalse(WipSlotReservation.objects.exists())
        heartbeat.assert_called_once_with(liveness.WIP_ADMISSION)

    def test_admission_failure_does_not_emit_false_success_heartbeat(self):
        """DB/lock failure must leave the liveness timestamp stale."""
        from tasks.models import AgentWipCapacity, Item

        candidate = Item.objects.create(
            title='broken boundary candidate', mode='task', status='open',
            priority='urgent', assignment_group='review',
            assignee_id=self.UID, assignee_email=self.EMAIL,
            workflow_name='autonomous-dev', project=self.project,
        )
        with patch.object(
            AgentWipCapacity.objects,
            'get_or_create',
            side_effect=RuntimeError('capacity DB unavailable'),
        ), patch('tasks.liveness.record_heartbeat') as heartbeat:
            with self.assertRaisesRegex(RuntimeError, 'capacity DB unavailable'):
                WorkflowService._reserve_wip_slot(
                    candidate,
                    self.UID,
                    self.EMAIL,
                    status_override='in_review',
                )
        heartbeat.assert_not_called()

    def test_capacity_row_lock_timeout_is_logged_distinctly_from_denial(self):
        """PLS-912: lock failure is an operational signal, never cap pressure."""
        from django.db import OperationalError
        from tasks.models import AgentWipCapacity, Item

        AgentWipCapacity.objects.create(
            organization_slug='shizuha',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
        )
        candidate = Item.objects.create(
            title='capacity lock timeout signal', mode='task', status='open',
            priority='urgent', assignment_group='review',
            assignee_id=self.UID, assignee_email=self.EMAIL,
            workflow_name='autonomous-dev', project=self.project,
        )
        error = OperationalError(
            'canceling statement due to lock timeout while locking tuple in '
            'relation "tasks_agentwipcapacity"'
        )
        with patch.object(
            AgentWipCapacity.objects,
            'select_for_update',
            side_effect=error,
        ), self.assertLogs(
            'tasks.services.workflow_service', level='ERROR',
        ) as captured, self.assertRaises(OperationalError):
            WorkflowService._reserve_wip_slot(
                candidate, self.UID, self.EMAIL,
            )

        joined = '\n'.join(captured.output)
        self.assertIn('event=wip_capacity_lock_timeout', joined)
        self.assertNotIn('wip_capacity_denied', joined)

    def test_same_item_retry_replaces_orphaned_reservation(self):
        """A lost resolver caller cannot make its own item capacity-blocked."""
        from tasks.models import Item, WipSlotReservation

        item = Item.objects.create(
            title='retry same blocker admission',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='review',
            workflow_name='autonomous-dev',
            project=self.project,
        )
        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 return_value=True,
             ):
            self.assertEqual(
                WorkflowService._resolve_team_assignee('review', item=item),
                (self.UID, self.EMAIL),
            )
            first_token = item._wip_reservation_token
            self.assertEqual(WipSlotReservation.objects.count(), 1)

            self.assertEqual(
                WorkflowService._resolve_team_assignee('review', item=item),
                (self.UID, self.EMAIL),
            )
            self.assertNotEqual(item._wip_reservation_token, first_token)
            self.assertEqual(WipSlotReservation.objects.count(), 1)

            item.assign({'id': self.UID, 'email': self.EMAIL}, save=True)
            self.assertFalse(WipSlotReservation.objects.exists())

    def test_hibernated_wakeable_seat_commits_wip_reservation(self):
        """Production order: typed wakeability -> resolve -> reserve -> assign."""
        from tasks.models import Item, WipSlotReservation

        item = Item.objects.create(
            title='wake hibernated architecture seat',
            mode='task', status='open', priority='urgent',
            assignment_group='review', workflow_name='autonomous-dev',
            project=self.project,
        )
        with patch('tasks.user_service.user_service', self._user_service()), \
                patch.object(
                    WorkflowService, '_health_availability_map',
                    return_value={self.EMAIL: False},
                ), patch.object(
                    WorkflowService, '_wakeable_agent_emails',
                    return_value={self.EMAIL},
                ), patch.object(
                    WorkflowService, '_disabled_agent_emails', return_value=set(),
                ), patch.object(
                    WorkflowService, '_db_agent_availability', return_value=None,
                ), patch.object(
                    WorkflowService, '_configured_email_set', return_value=set(),
                ):
            new_assignment = WorkflowService._seat_routability_verdict(
                item,
                'review',
                self.EMAIL,
                intent='new_assignment',
            )
            resident_capacity = WorkflowService._seat_routability_verdict(
                item,
                'review',
                self.EMAIL,
                intent='owner_drain',
            )
            self.assertTrue(new_assignment.ok, new_assignment.reasons)
            self.assertFalse(resident_capacity.ok)
            self.assertIn('seat_not_live', resident_capacity.reasons)
            self.assertEqual(
                WorkflowService._resolve_team_assignee('review', item=item),
                (self.UID, self.EMAIL),
            )
            self.assertTrue(getattr(item, '_wip_reservation_token', None))
            item.assign({'id': self.UID, 'email': self.EMAIL}, save=True)

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, self.EMAIL)
        self.assertFalse(WipSlotReservation.objects.exists())

    def test_reservation_ttl_is_bounded_and_malformed_env_falls_back(self):
        from tasks.models import Item, WipSlotReservation

        for configured in ('999999', 'not-an-integer'):
            item = Item.objects.create(
                title=f'bounded ttl {configured}',
                mode='task',
                status='open',
                priority='urgent',
                assignment_group='review',
                workflow_name='autonomous-dev',
                project=self.project,
            )
            with patch.dict(
                os.environ,
                {'PULSE_WIP_RESERVATION_TTL_SECONDS': configured},
            ):
                self.assertTrue(
                    WorkflowService._reserve_wip_slot(
                        item,
                        self.UID,
                        self.EMAIL,
                    )
                )
            reservation = WipSlotReservation.objects.get(
                token=item._wip_reservation_token,
            )
            lifetime = (
                reservation.expires_at - reservation.created_at
            ).total_seconds()
            self.assertGreaterEqual(lifetime, 14)
            self.assertLessEqual(
                lifetime,
                WorkflowService._WIP_RESERVATION_MAX_AGE_SECONDS + 1,
            )
            WorkflowService._release_unconsumed_wip_reservation(item)

    def test_created_at_hard_ceiling_excludes_corrupt_future_expiry(self):
        from datetime import timedelta

        from django.core.exceptions import ValidationError
        from django.utils import timezone

        from tasks.models import Item, WipSlotReservation

        stale = Item.objects.create(
            title='corrupt future-expiry reservation',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='review',
            workflow_name='autonomous-dev',
            project=self.project,
        )
        self.assertTrue(
            WorkflowService._reserve_wip_slot(
                stale,
                self.UID,
                self.EMAIL,
            )
        )
        stale_token = stale._wip_reservation_token
        now = timezone.now()
        WipSlotReservation.objects.filter(token=stale_token).update(
            created_at=now - timedelta(
                seconds=WorkflowService._WIP_RESERVATION_MAX_AGE_SECONDS + 1,
            ),
            expires_at=now + timedelta(days=30),
        )
        stale.assignee_id = self.UID
        stale.assignee_email = self.EMAIL
        with transaction.atomic():
            with self.assertRaises(ValidationError):
                WorkflowService._verify_wip_reservation(stale)

        fresh = Item.objects.create(
            title='fresh reservation after corrupt row',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='review',
            workflow_name='autonomous-dev',
            project=self.project,
        )
        self.assertTrue(
            WorkflowService._reserve_wip_slot(
                fresh,
                self.UID,
                self.EMAIL,
            )
        )
        self.assertFalse(
            WipSlotReservation.objects.filter(token=stale_token).exists()
        )
        self.assertEqual(WipSlotReservation.objects.count(), 1)
        WorkflowService._release_unconsumed_wip_reservation(fresh)

    def test_transition_early_return_releases_unconsumed_reservation(self):
        """Recurring Run Now-style returns cannot leak their preclaimed seat."""
        from tasks.models import Item, WipSlotReservation

        item = Item.objects.create(
            title='early return reservation owner',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='review',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )
        self.assertTrue(
            WorkflowService._reserve_wip_slot(
                item,
                self.UID,
                self.EMAIL,
                status_override='in_review',
            )
        )
        with patch.object(
            WorkflowService,
            '_execute_transition_with_dependency_lock',
            return_value=item,
        ):
            self.assertIs(
                WorkflowService.execute_transition(item, 'in_review'),
                item,
            )

        self.assertFalse(WipSlotReservation.objects.exists())
        self.assertFalse(hasattr(item, '_wip_reservation_token'))

    def test_transition_exception_releases_unconsumed_reservation(self):
        """A failure after admission releases the exact owned token now."""
        from tasks.models import Item, WipSlotReservation

        item = Item.objects.create(
            title='exception reservation owner',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='review',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )
        self.assertTrue(
            WorkflowService._reserve_wip_slot(
                item,
                self.UID,
                self.EMAIL,
                status_override='in_review',
            )
        )
        with patch.object(
            WorkflowService,
            '_execute_transition_with_dependency_lock',
            side_effect=RuntimeError('post-admission failure'),
        ), self.assertRaisesRegex(RuntimeError, 'post-admission failure'):
            WorkflowService.execute_transition(item, 'in_review')

        self.assertFalse(WipSlotReservation.objects.exists())
        self.assertFalse(hasattr(item, '_wip_reservation_token'))

    def test_resolver_then_exact_start_failure_cleanup_and_retry(self):
        """Production order: offer/assign → exact Start, fail, retry Start."""
        from tasks.models import (
            Item,
            Status,
            WipSlotReservation,
            Workflow,
            WorkflowStatus,
            WorkflowTransition,
        )

        open_status, _ = Status.objects.get_or_create(
            slug='open',
            defaults={'name': 'Open', 'category': 'todo'},
        )
        active_status, _ = Status.objects.get_or_create(
            slug='in_progress',
            defaults={'name': 'In Progress', 'category': 'in_progress'},
        )
        workflow = Workflow.objects.create(
            name='PLS 509 production sequence',
            slug='pls-509-production-sequence',
            initial_status=open_status,
            is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=open_status, position=0,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=active_status, position=1,
        )
        start = WorkflowTransition.objects.create(
            workflow=workflow,
            name='Start Progress',
            from_status='open',
            to_status='in_progress',
            position=0,
        )
        item = Item.objects.create(
            title='resolver exact-start retry', mode='task', status='open',
            priority='urgent', assignment_group='review',
            workflow_name=workflow.slug, project=self.project,
        )

        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 return_value=True,
             ):
            self.assertEqual(
                WorkflowService._resolve_team_assignee('review', item=item),
                (self.UID, self.EMAIL),
            )
            item.assign({'id': self.UID, 'email': self.EMAIL}, save=True)
            self.assertFalse(WipSlotReservation.objects.exists())

            with patch.object(
                Item,
                'save',
                autospec=True,
                side_effect=RuntimeError('failed after active reservation'),
            ), self.assertRaisesRegex(
                RuntimeError, 'failed after active reservation',
            ):
                WorkflowService.execute_transition(
                    item,
                    'in_progress',
                    transition_id=str(start.id),
                )
            self.assertFalse(WipSlotReservation.objects.exists())

            item.refresh_from_db()
            WorkflowService.execute_transition(
                item,
                'in_progress',
                transition_id=str(start.id),
            )

        item.refresh_from_db()
        self.assertEqual(item.status, 'in_progress')
        self.assertFalse(WipSlotReservation.objects.exists())

    def test_direct_assignment_validation_failure_releases_then_retries(self):
        """Direct /assign reserve → validation failure → immediate retry."""
        from tasks.models import Item, WipSlotReservation

        actor = get_user_model().objects.create_user(
            username='pls509-assignment-actor',
            email='pls509-assignment-actor@example.com',
            password='unused',
            is_staff=True,
        )
        client = APIClient()
        client.force_authenticate(user=actor)
        item = Item.objects.create(
            title='direct assignment cleanup retry',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='review',
            workflow_name='autonomous-dev',
            project=self.project,
        )
        payload = {
            'assignee_email': self.EMAIL,
            'assignment_group': 'review',
            'direct_assignment_reason': 'PLS-509 cleanup regression',
        }
        mismatch_calls = 0

        def fail_first_org_check(*_args, **_kwargs):
            nonlocal mismatch_calls
            mismatch_calls += 1
            if mismatch_calls == 1:
                return True, 'wrong-org', 'shizuha'
            return False, 'shizuha', 'shizuha'

        with patch('tasks.views.user_service', self._user_service()), \
             patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 return_value=True,
             ), \
             patch.object(
                 WorkflowService,
                 '_agent_at_capacity',
                 return_value=False,
             ), \
             patch.object(
                 WorkflowService,
                 'assignment_org_mismatch',
                 side_effect=fail_first_org_check,
             ):
            denied = client.post(
                f'/api/items/{item.pk}/assign/', payload, format='json',
            )
            self.assertEqual(denied.status_code, 400, denied.content)
            self.assertFalse(WipSlotReservation.objects.exists())

            retried = client.post(
                f'/api/items/{item.pk}/assign/', payload, format='json',
            )

        self.assertEqual(retried.status_code, 200, retried.content)
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, self.EMAIL)
        self.assertFalse(WipSlotReservation.objects.exists())

    def _enable_wip_source_team(self, source_email='source-seat@example.com'):
        """Make the persisted source lane reservation-capable like live DevOps."""
        from tasks.models import TeamRoster

        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='architecture',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[source_email]],
                'junior_emails': [source_email],
                'is_active': True,
                'wip_enforced': True,
                'ready_wip_cap': 2,
                'active_wip_cap': 2,
            },
        )
        WorkflowService._invalidate_team_roster_caches()

    def _cross_team_transition_fixture(self):
        """ORIG-127 shape: WIP source lane → assign_team:architecture."""
        from tasks.models import (
            Item,
            Status,
            TeamRoster,
            Workflow,
            WorkflowStatus,
            WorkflowTransition,
        )

        actor = get_user_model().objects.create_user(
            username='pls567-transition-actor',
            email='pls567-transition-actor@example.com',
            password='unused',
            is_staff=True,
        )
        for team_name, email in (
            ('engineering', actor.email),
            ('architecture', self.EMAIL),
        ):
            TeamRoster.objects.update_or_create(
                organization_id=1,
                team_name=team_name,
                defaults={
                    'organization_slug': 'shizuha',
                    'tiers': [[email]],
                    'junior_emails': [email],
                    'is_active': True,
                    'wip_enforced': True,
                    'ready_wip_cap': 2,
                    'active_wip_cap': 2,
                },
            )
        WorkflowService._invalidate_team_roster_caches()

        needs_triage, _ = Status.objects.get_or_create(
            slug='needs_triage',
            defaults={'name': 'Needs Triage', 'category': 'todo'},
        )
        open_status, _ = Status.objects.get_or_create(
            slug='open',
            defaults={'name': 'Open', 'category': 'todo'},
        )
        workflow = Workflow.objects.create(
            name='PLS 567 transition handoff',
            slug='pls-567-transition-handoff',
            initial_status=needs_triage,
            is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=needs_triage, position=0,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=open_status, position=1,
        )
        transition = WorkflowTransition.objects.create(
            workflow=workflow,
            name='Triage to Architecture',
            from_status='needs_triage',
            to_status='open',
            position=0,
            post_functions=[{'type': 'assign_team', 'value': 'architecture'}],
        )
        item = Item.objects.create(
            title='ORIG-127 transition-path reproduction',
            mode='task',
            status='needs_triage',
            workflow_status='Needs Triage',
            priority='high',
            assignment_group='engineering',
            workflow_name=workflow.slug,
            project=self.project,
        )
        Item.objects.filter(pk=item.pk).update(
            assignee_id=actor.id,
            assignee_email=actor.email,
            created_by_id=actor.id,
            created_by_email=actor.email,
        )
        item.refresh_from_db()
        return actor, item, transition

    def test_cross_team_transition_consumes_destination_reservation_once(self):
        """Real transition post-function commits one status/assignment pair."""
        from tasks.models import ItemActivity, WipSlotReservation

        actor, item, transition = self._cross_team_transition_fixture()
        client = APIClient()
        client.force_authenticate(user=actor)

        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 return_value=True,
             ):
            response = client.post(
                f'/api/items/{item.pk}/execute-transition/',
                {'transition_id': str(transition.id)},
                format='json',
            )

        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertEqual(item.status, 'open')
        self.assertEqual(item.assignment_group, 'architecture')
        self.assertEqual(item.assignee_id, self.UID)
        self.assertEqual(item.assignee_email, self.EMAIL)
        self.assertFalse(WipSlotReservation.objects.exists())
        self.assertEqual(
            ItemActivity.objects.filter(
                item=item, action='status_changed',
            ).count(),
            1,
        )
        self.assertEqual(
            ItemActivity.objects.filter(item=item, action='assigned').count(),
            1,
        )

    def test_cross_team_transition_to_full_destination_commits_group_queue(self):
        """PLS-618: source owner -> full destination -> atomic null owner."""
        from tasks.models import Comment, Item, ItemActivity, WipSlotReservation

        actor, item, transition = self._cross_team_transition_fixture()
        client = APIClient()
        client.force_authenticate(user=actor)

        # Production order: the source task is concretely DevOps/Engineering
        # owned, both Architecture ready slots are already consumed, and the
        # real execute-transition endpoint runs the assign_team post-function.
        for index in range(2):
            Item.objects.create(
                title=f'architecture ready slot {index}',
                mode='task',
                status='open',
                priority='normal',
                assignment_group='architecture',
                assignee_id=self.UID,
                assignee_email=self.EMAIL,
                workflow_name='autonomous-dev',
                project=self.project,
            )

        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 return_value=True,
             ):
            response = client.post(
                f'/api/items/{item.pk}/execute-transition/',
                {'transition_id': str(transition.id)},
                format='json',
            )

        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertEqual(item.status, 'open')
        self.assertEqual(item.assignment_group, 'architecture')
        self.assertIsNone(item.assignee_id)
        self.assertFalse(item.assignee_email)
        self.assertFalse(WipSlotReservation.objects.exists())
        self.assertTrue(Comment.objects.filter(
            item=item,
            origin_event='resolver.routability_hold',
        ).exists())
        self.assertEqual(
            ItemActivity.objects.filter(
                item=item, action='status_changed',
            ).count(),
            1,
        )

    def test_cross_team_transition_validation_is_typed_and_rolls_back(self):
        """Final handoff denial is HTTP 409 with no partial state or token."""
        from django.core.exceptions import ValidationError
        from tasks.models import Item, ItemActivity, WipSlotReservation

        actor, item, transition = self._cross_team_transition_fixture()
        client = APIClient()
        client.force_authenticate(user=actor)
        real_save = Item.save

        def deny_final_handoff(instance, *args, **kwargs):
            if getattr(instance, '_wip_reservation_token', None):
                raise ValidationError({
                    'assignee_id': 'wip_reservation_target_mismatch',
                })
            return real_save(instance, *args, **kwargs)

        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 return_value=True,
             ), \
             patch.object(Item, 'save', autospec=True, side_effect=deny_final_handoff):
            response = client.post(
                f'/api/items/{item.pk}/execute-transition/',
                {'transition_id': str(transition.id)},
                format='json',
            )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.data['error'], 'transition_validation_failed')
        self.assertEqual(
            response.data['details']['assignee_id'],
            ['wip_reservation_target_mismatch'],
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'needs_triage')
        self.assertEqual(item.assignment_group, 'engineering')
        self.assertEqual(item.assignee_id, actor.id)
        self.assertEqual(item.assignee_email, actor.email)
        self.assertFalse(WipSlotReservation.objects.exists())
        self.assertFalse(ItemActivity.objects.filter(item=item).exists())

    def test_cross_team_resolver_reserves_destination_lane(self):
        """PLS-567: Architecture→Review reserves Review, never stale source."""
        from tasks.models import Item, WipSlotReservation

        self._enable_wip_source_team()
        item = Item.objects.create(
            title='cross-team destination reservation',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='architecture',
            workflow_name='autonomous-dev',
            project=self.project,
        )

        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 return_value=True,
            ):
            self.assertEqual(
                WorkflowService._resolve_team_assignee(
                    'review',
                    item=item,
                    assignment_group_override='review',
                ),
                (self.UID, self.EMAIL),
            )

        reservation = WipSlotReservation.objects.get(
            token=item._wip_reservation_token,
        )
        self.assertEqual(reservation.assignment_group, 'review')
        item.assignment_group = 'review'
        item.assign({'id': self.UID, 'email': self.EMAIL}, save=False)
        item.save()
        self.assertFalse(WipSlotReservation.objects.exists())

    def test_working_backstop_fallback_does_not_reserve_other_roster(self):
        """A selector fallback must not mint a reservation for another lane."""
        from tasks.models import Item, TeamRoster, WipSlotReservation

        for team_name, email, wip_enforced in (
            ('architecture', 'source-seat@example.com', False),
            ('engineering', self.EMAIL, True),
        ):
            TeamRoster.objects.update_or_create(
                organization_id=1,
                team_name=team_name,
                defaults={
                    'organization_slug': 'shizuha',
                    'tiers': [[email]],
                    'junior_emails': [email],
                    'is_active': True,
                    'wip_enforced': wip_enforced,
                    'ready_wip_cap': 2,
                    'active_wip_cap': 2,
                },
            )
        WorkflowService._invalidate_team_roster_caches()
        item = Item.objects.create(
            title='working backstop selector without reroute',
            mode='task',
            status='in_progress',
            priority='urgent',
            assignment_group='architecture',
            workflow_name='autonomous-dev',
            project=self.project,
            created_by_email='author@example.com',
        )

        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 side_effect=lambda email: email == self.EMAIL,
             ):
            WorkflowService._ensure_assigned_if_working(item)

        item.refresh_from_db()
        self.assertEqual(item.assignment_group, 'architecture')
        self.assertIsNone(item.assignee_id)
        self.assertFalse(item.assignee_email)
        self.assertFalse(WipSlotReservation.objects.exists())

    def test_cross_team_assign_api_consumes_destination_reservation(self):
        """Production order: team resolve → REROUTE tuple → one atomic save."""
        from tasks.models import Item, WipSlotReservation

        actor = get_user_model().objects.create_user(
            username='pls567-assignment-actor',
            email='pls567-assignment-actor@example.com',
            password='unused',
            is_staff=True,
        )
        self._enable_wip_source_team(actor.email)
        client = APIClient()
        client.force_authenticate(user=actor)
        item = Item.objects.create(
            title='PLS-567 live cross-team shape',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='architecture',
            workflow_name='autonomous-dev',
            project=self.project,
        )
        # Model-level org/team authorization is intentionally fail-closed. The
        # incident fixture starts from an already-persisted source owner, so
        # inject that pre-existing tuple without exercising a separate create-
        # time routing contract.
        Item.objects.filter(pk=item.pk).update(
            assignee_id=actor.id,
            assignee_email=actor.email,
            created_by_id=actor.id,
            created_by_email=actor.email,
        )
        item.refresh_from_db()

        with patch('tasks.views.user_service', self._user_service()), \
             patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 return_value=True,
             ), \
             patch.object(
                 WorkflowService,
                 '_member_can_execute_from_status',
                 return_value=True,
             ), \
             patch.object(
                 WorkflowService,
                 'assignment_org_mismatch',
                 return_value=(False, 'shizuha', 'shizuha'),
             ):
            response = client.post(
                f'/api/items/{item.pk}/assign/',
                {'assignment_group': 'review'},
                format='json',
            )

        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertEqual(item.assignment_group, 'review')
        self.assertEqual(item.assignee_id, self.UID)
        self.assertEqual(item.assignee_email, self.EMAIL)
        self.assertFalse(WipSlotReservation.objects.exists())

    def test_cross_team_wip_validation_is_typed_and_releases_token(self):
        """An expected admission rejection is 409 and retry-ready, not HTTP 500."""
        from django.core.exceptions import ValidationError
        from tasks.models import Item, WipSlotReservation

        actor = get_user_model().objects.create_user(
            username='pls567-validation-actor',
            email='pls567-validation-actor@example.com',
            password='unused',
            is_staff=True,
        )
        self._enable_wip_source_team(actor.email)
        client = APIClient()
        client.force_authenticate(user=actor)
        item = Item.objects.create(
            title='typed cross-team admission denial',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='architecture',
            workflow_name='autonomous-dev',
            project=self.project,
        )
        Item.objects.filter(pk=item.pk).update(
            assignee_id=actor.id,
            assignee_email=actor.email,
            created_by_id=actor.id,
            created_by_email=actor.email,
        )
        item.refresh_from_db()

        with patch('tasks.views.user_service', self._user_service()), \
             patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(
                 WorkflowService,
                 '_is_agent_available_for_assignment',
                 return_value=True,
             ), \
             patch.object(
                 WorkflowService,
                 '_member_can_execute_from_status',
                 return_value=True,
             ), \
             patch.object(
                 WorkflowService,
                 'assignment_org_mismatch',
                 return_value=(False, 'shizuha', 'shizuha'),
             ), \
             patch.object(
                 Item,
                 'save',
                 autospec=True,
                 side_effect=ValidationError({
                     'assignee_id': 'wip_reservation_target_mismatch',
                 }),
             ):
            response = client.post(
                f'/api/items/{item.pk}/assign/',
                {'assignment_group': 'review'},
                format='json',
            )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.data['error'], 'assignment_validation_failed')
        self.assertEqual(
            response.data['details']['assignee_id'],
            ['wip_reservation_target_mismatch'],
        )
        self.assertFalse(WipSlotReservation.objects.exists())

    def test_blocked_display_category_never_consumes_or_loses_active_slot(self):
        """Fine-grained parked status wins over its in_progress UI category."""
        from tasks.models import Item

        active = Item.objects.create(
            title='actual active seat', mode='task', status='in_review',
            priority='normal', assignment_group='review', assignee_id=self.UID,
            assignee_email=self.EMAIL, workflow_name='autonomous-dev', project=self.project,
        )
        blocked = create_blocked_item(
            title='dependency blocked seat', mode='task', priority='urgent',
            assignment_group='review', assignee_id=self.UID,
            assignee_email=self.EMAIL, workflow_name='autonomous-dev', project=self.project,
        )

        self.assertNotIn('blocked', WorkflowService._slot_statuses('active'))
        self.assertEqual(
            WorkflowService._agent_slot_load(active, self.EMAIL, 'active'),
            0,
        )
        result = _reclaim_wip_overload()

        blocked.refresh_from_db()
        self.assertEqual(result['active_queued'], 0)
        self.assertEqual(blocked.status, 'blocked')
        self.assertEqual(blocked.assignee_email, self.EMAIL)

    def test_twenty_concurrent_resolvers_admit_exactly_five(self):
        from tasks.models import Item, TeamRoster, WipSlotReservation
        TeamRoster.objects.filter(
            organization_slug='shizuha', team_name='review',
        ).update(ready_wip_cap=5)
        WorkflowService._invalidate_team_roster_caches()
        items = [Item.objects.create(
            title=f'concurrent-{idx}', mode='task', status='open',
            priority='high' if idx < 3 else 'normal',
            assignment_group='review', workflow_name='autonomous-dev', project=self.project,
        ) for idx in range(20)]
        barrier = Barrier(len(items))

        def resolve(item):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                result = WorkflowService._resolve_team_assignee('review', item=item)
                return item, result
            finally:
                close_old_connections()

        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True), \
             ThreadPoolExecutor(max_workers=len(items)) as pool:
            results = list(pool.map(resolve, items))

        winners = [(item, result) for item, result in results if result[0] == self.UID]
        self.assertEqual(len(winners), 5)
        self.assertEqual(WipSlotReservation.objects.count(), 5)
        for item, _result in winners:
            item.assign({'id': self.UID, 'email': self.EMAIL}, save=True)
        self.assertEqual(Item.objects.filter(assignee_email=self.EMAIL).count(), 5)
        self.assertFalse(WipSlotReservation.objects.exists())

    def test_recovery_preserves_one_active_and_urgent_fifo(self):
        from tasks.models import Item
        created = []
        for title, priority in (
            ('normal-oldest', 'normal'), ('urgent-oldest', 'urgent'), ('urgent-newest', 'urgent')
        ):
            created.append(Item.objects.create(
                title=title, mode='task', status='in_review', priority=priority,
                assignment_group='review', assignee_id=self.UID,
                assignee_email=self.EMAIL, workflow_name='autonomous-dev', project=self.project,
            ))
        with patch('tasks.user_service.user_service', self._user_service()):
            result = _reclaim_wip_overload()
        assigned = list(Item.objects.filter(
            pk__in=[i.pk for i in created], assignee_email=self.EMAIL,
        ))
        queued = WorkflowService._queued_tasks_for_team('review')
        # PLAT-390 two-tier: review-pipeline items are reclaimed on the READY
        # dimension (in-flight, not hands-on), not active.
        self.assertEqual(result['ready_queued'], 2)
        self.assertEqual(len(assigned), 1)
        self.assertEqual(assigned[0].title, 'urgent-oldest')
        self.assertEqual(
            [i.title for i in queued[:2]],
            ['urgent-newest', 'normal-oldest'],
        )
        self.assertTrue(all(i.status == 'in_review' for i in Item.objects.filter(pk__in=[x.pk for x in created])))

    def test_pls922_exact_org_disabled_ready_cap_is_not_reclaimed_by_global_cap(self):
        """PLS-922: an exact-org roster with ready_wip_cap=0 (disabled) must not
        have its ready items reclaimed by the GLOBAL shizuha::devops roster cap.

        Production caller sequence (mirrors the live BKST-6/BKST-7 oscillation):
        ichi sits on BOTH the global `shizuha::devops` roster (ready_wip_cap=5)
        and the exact-org `shizuha-trading::devops` roster (ready_wip_cap=0
        disabled). Detector + capacity-pull resolve the exact-org roster
        (ready_wip_cap=0 -> member available -> pulls BKST-6/7 to ichi); reclaim
        must use the SAME exact-org contract and leave the trading-org ready
        items alone instead of returning them to the queue (the false
        pull->return oscillation). Two consecutive sweeps must be a fixed point.
        """
        from tasks.models import Item, Project, TeamRoster

        trading = Project.objects.create(
            org_id=3, name='Shizuha Trading', key='TRD',
        )
        # The member is NOT a junior (junior-scope guard would filter them out
        # of devops); clear the setUp review roster's junior_emails marker.
        TeamRoster.objects.filter(
            organization_id=1, team_name='review',
        ).update(junior_emails=[])
        WorkflowService._invalidate_team_roster_caches()
        # Global shizuha::devops roster — generous ready cap (5).
        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='devops',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.EMAIL]], 'is_active': True,
                'wip_enforced': True, 'ready_wip_cap': 5, 'active_wip_cap': 10,
            },
        )
        # Exact-org shizuha-trading::devops roster — ready cap DISABLED (0).
        TeamRoster.objects.update_or_create(
            organization_id=3, team_name='devops',
            defaults={
                'organization_slug': 'shizuha-trading',
                'tiers': [[self.EMAIL]], 'is_active': True,
                'wip_enforced': True, 'ready_wip_cap': 0, 'active_wip_cap': 0,
            },
        )
        WorkflowService._invalidate_team_roster_caches()

        # ichi owns 6 ready items in the GLOBAL org (over the 5-cap) and 2 ready
        # items in the trading org (BKST-6/BKST-7 analogues).
        for idx in range(6):
            Item.objects.create(
                title=f'global-ready-{idx}', mode='task', status='open',
                priority='normal', assignment_group='devops',
                assignee_id=self.UID, assignee_email=self.EMAIL,
                workflow_name='autonomous-dev', project=self.project,
            )
        for idx in range(2):
            Item.objects.create(
                title=f'trading-ready-{idx}', mode='task', status='open',
                priority='normal', assignment_group='devops',
                assignee_id=self.UID, assignee_email=self.EMAIL,
                workflow_name='autonomous-dev', project=trading,
            )

        with patch('tasks.user_service.user_service', self._user_service()):
            first = _reclaim_wip_overload()
            second = _reclaim_wip_overload()

        # The GLOBAL cap reclaims global-org ready overflow (6 -> 5 kept).
        self.assertEqual(first['ready_queued'], 1)
        # The exact-org trading roster's ready_wip_cap=0 means NO ready reclaim
        # for trading-org items — this is the fixed point that stops the
        # oscillation. A second sweep must be a no-op (no duplicate movement).
        self.assertEqual(second['ready_queued'], 0)
        trading_kept = Item.objects.filter(
            project=trading, assignee_email=self.EMAIL,
        ).count()
        self.assertEqual(trading_kept, 2)
        global_kept = Item.objects.filter(
            project=self.project, assignee_email=self.EMAIL,
        ).count()
        self.assertEqual(global_kept, 5)

    def test_pls922_below_cap_capacity_still_pulls_work(self):
        """PLS-922: org-scoping reclaim must not break genuine below-cap pulls.

        A member under the exact-org ready cap is still pullable: the capacity
        resolver admits them for new work (no false over-capacity), and reclaim
        does not return the item they just pulled.
        """
        from tasks.models import Item, Project, TeamRoster

        trading = Project.objects.create(
            org_id=3, name='Shizuha Trading', key='TRD2',
        )
        # Member is not a junior; clear the setUp review roster marker.
        TeamRoster.objects.filter(
            organization_id=1, team_name='review',
        ).update(junior_emails=[])
        WorkflowService._invalidate_team_roster_caches()
        TeamRoster.objects.update_or_create(
            organization_id=3, team_name='devops',
            defaults={
                'organization_slug': 'shizuha-trading',
                'tiers': [[self.EMAIL]], 'is_active': True,
                'wip_enforced': True, 'ready_wip_cap': 5, 'active_wip_cap': 10,
            },
        )
        WorkflowService._invalidate_team_roster_caches()

        # ichi owns 3 ready items in the trading org (below the 5-cap).
        for idx in range(3):
            Item.objects.create(
                title=f'under-cap-ready-{idx}', mode='task', status='open',
                priority='normal', assignment_group='devops',
                assignee_id=self.UID, assignee_email=self.EMAIL,
                workflow_name='autonomous-dev', project=trading,
            )
        new_item = Item.objects.create(
            title='pullable-new-work', mode='task', status='open',
            priority='urgent', assignment_group='devops',
            workflow_name='autonomous-dev', project=trading,
        )

        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            # Capacity-pull admits ichi for new work (under the exact-org cap).
            resolved = WorkflowService._resolve_team_assignee(
                'devops', item=new_item, organization_id=3,
            )
            self.assertEqual(resolved, (self.UID, self.EMAIL))
            # Reclaim leaves the under-cap ready items alone.
            result = _reclaim_wip_overload()
            self.assertEqual(result['ready_queued'], 0)
        self.assertEqual(
            Item.objects.filter(project=trading, assignee_email=self.EMAIL).count(),
            3,
        )

    def test_recovery_never_demotes_or_unassigns_blocker_root(self):
        """Capacity is reclaimed around a dependency root, never through it."""
        from tasks.models import Item

        root = Item.objects.create(
            title='normal active blocker root',
            mode='task',
            status='in_review',
            priority='normal',
            assignment_group='review',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )
        create_blocked_item(
            blocker=root,
            title='urgent blocked descendant',
            mode='task',
            priority='urgent',
            assignment_group='review',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )
        ordinary = Item.objects.create(
            title='unrelated urgent active work',
            mode='task',
            status='in_review',
            priority='urgent',
            assignment_group='review',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )

        result = _reclaim_wip_overload()

        root.refresh_from_db()
        ordinary.refresh_from_db()
        self.assertEqual(root.status, 'in_review')
        self.assertEqual(root.assignee_email, self.EMAIL)
        self.assertEqual(ordinary.status, 'in_review')
        self.assertFalse(ordinary.assignee_email)
        # PLAT-390 two-tier: review-pipeline items are reclaimed on the READY
        # dimension (in-flight, not hands-on), not active. The blocker root is
        # still protected; the unrelated ordinary review is reclaimed once.
        self.assertEqual(result['ready_queued'], 1)
        self.assertEqual(result['active_queued'], 0)

    def test_recovery_rolls_back_failed_transition_and_continues(self):
        from tasks.models import (
            Item, Status, Workflow, WorkflowStatus, WorkflowTransition,
        )

        open_status, _ = Status.objects.get_or_create(
            slug='open',
            defaults={'name': 'Open', 'category': 'todo'},
        )
        in_progress_status, _ = Status.objects.get_or_create(
            slug='in_progress',
            defaults={'name': 'In Progress', 'category': 'in_progress'},
        )
        workflow = Workflow.objects.create(
            name='Recovery Atomicity',
            slug='recovery-atomicity',
            initial_status=open_status,
            is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=open_status, position=0,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=in_progress_status, position=1,
        )
        WorkflowTransition.objects.create(
            workflow=workflow,
            name='Stop Progress',
            from_status='in_progress',
            to_status='open',
            position=0,
        )

        keeper = Item.objects.create(
            title='urgent keeper', mode='task', status='in_progress',
            priority='urgent', assignment_group='review', assignee_id=self.UID,
            assignee_email=self.EMAIL, workflow_name=workflow.slug,
            project=self.project,
        )
        bad = Item.objects.create(
            title='bad post-function', mode='task', status='in_progress',
            priority='high', assignment_group='review', assignee_id=self.UID,
            assignee_email=self.EMAIL, workflow_name=workflow.slug,
            project=self.project,
        )
        good = Item.objects.create(
            title='later healthy transition', mode='task', status='in_progress',
            priority='normal', assignment_group='review', assignee_id=self.UID,
            assignee_email=self.EMAIL, workflow_name=workflow.slug,
            project=self.project,
        )
        real_post_functions = WorkflowService.execute_post_functions

        def post_functions_with_one_failure(transition, item, user=None, screen_data=None):
            if item.pk == bad.pk:
                Item.objects.filter(pk=item.pk).update(
                    metadata={'partial_post_function_write': True},
                )
                raise IntegrityError('simulated post-function failure')
            return real_post_functions(
                transition, item, user=user, screen_data=screen_data,
            )

        with patch.object(
            WorkflowService,
            'execute_post_functions',
            side_effect=post_functions_with_one_failure,
        ) as execute_post_functions:
            result = _reclaim_wip_overload()

        keeper.refresh_from_db()
        bad.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(execute_post_functions.call_count, 2)
        self.assertEqual(result['active_queued'], 1)
        self.assertEqual(result['active_stopped'], 1)
        self.assertEqual(keeper.status, 'in_progress')
        self.assertEqual(bad.status, 'in_progress')
        self.assertNotIn('partial_post_function_write', bad.metadata or {})
        self.assertIsNone(bad.assignee_id)
        self.assertIsNone(bad.assignee_email)
        self.assertEqual(good.status, 'open')

    def test_recovery_keeps_reviewer_alias_on_review_lane(self):
        """A roster @shizuha.com member assigned as @agents.shizuha.io stays."""
        from tasks.models import Item, TeamRoster
        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='review',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['reika@shizuha.com']],
                'is_active': True,
                'wip_enforced': True,
                'ready_wip_cap': 10,
                'active_wip_cap': 10,
            },
        )
        item = Item.objects.create(
            title='review stays with the canonical alias',
            mode='task',
            status='in_review',
            priority='high',
            assignment_group='review',
            assignee_id=143,
            assignee_email='reika@agents.shizuha.io',
            workflow_name='reviewed-task',
            project=self.project,
        )

        result = _reclaim_wip_overload()

        item.refresh_from_db()
        self.assertEqual(result['active_queued'], 0)
        self.assertEqual(item.assignee_email, 'reika@agents.shizuha.io')
        self.assertEqual(item.assignment_group, 'review')
        self.assertFalse(
            item.comments.filter(
                content__contains='Removed stale prior-stage owner',
            ).exists(),
        )

    def test_recovery_removes_stale_review_owner_from_qa_lane(self):
        from tasks.models import Item, TeamRoster
        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='qa',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['qa-seat@example.com']], 'is_active': True,
                'wip_enforced': True, 'ready_wip_cap': 5, 'active_wip_cap': 1,
            },
        )
        item = Item.objects.create(
            title='focused QA must not stay on reviewer', mode='task',
            status='in_qa', priority='urgent', assignment_group='review',
            assignee_id=self.UID, assignee_email=self.EMAIL,
            workflow_name='autonomous-dev', project=self.project,
        )

        result = _reclaim_wip_overload()

        item.refresh_from_db()
        self.assertEqual(result['active_queued'], 1)
        self.assertEqual(item.status, 'in_qa')
        self.assertEqual(item.assignment_group, 'qa')
        self.assertIsNone(item.assignee_id)
        self.assertIsNone(item.assignee_email)

    def test_recovery_preserves_org_local_focused_qa_review_fallback(self):
        from tasks.models import Item, Project, TeamRoster

        digital = Project.objects.create(
            org_id=2,
            name='Books Digital',
            key='BKSD',
        )
        TeamRoster.objects.create(
            organization_id=2,
            organization_slug='shizuha-digital',
            team_name='accounting',
            tiers=[['kane@shizuha.com']],
            is_active=True,
            wip_enforced=False,
        )
        item = Item.objects.create(
            title='tenant-local focused QA approval',
            mode='task',
            source='focused-qa-automation',
            status='in_review',
            priority='high',
            assignment_group='accounting',
            assignee_id=42,
            assignee_email='kane@shizuha.com',
            workflow_name='reviewed-task',
            project=digital,
            metadata={
                'focused_qa_routing_mode': 'origin-team-fallback',
                'focused_qa_assignment_group': 'accounting',
            },
        )

        result = _reclaim_wip_overload()

        item.refresh_from_db()
        self.assertEqual(result['active_queued'], 0)
        self.assertEqual(item.status, 'in_review')
        self.assertEqual(item.assignment_group, 'accounting')
        self.assertEqual(item.assignee_id, 42)
        self.assertEqual(item.assignee_email, 'kane@shizuha.com')
        self.assertFalse(
            item.comments.filter(
                content__contains='Removed stale prior-stage owner',
            ).exists(),
        )

    def test_recovery_honors_decision_pin_on_review_lane_item(self):
        """PLAT-7392: a PLS-233 decision pin (direct assignment WITH a recorded
        reason) on an in_review review-lane item must survive the stale-owner
        repair sweep — the pin marks a deliberate routing decision, not stale
        prior-stage ownership. The unpinned twin (same lane, same non-roster
        assignee, no pin) is still swept, proving the pin is the discriminator.
        """
        from tasks.models import Item, TeamRoster

        # The assignee is org-authorized (on the engineering roster) but NOT a
        # review-lane member — exactly the rei shape: capability-matched direct
        # assignment from outside the lane roster.
        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['rei@shizuha.com', 'rui@shizuha.com']],
                'is_active': True, 'wip_enforced': False,
            },
        )

        pinned = Item.objects.create(
            title='capability-matched direct review assignment',
            mode='task',
            status='in_review',
            priority='urgent',
            # Direct-override shape (PLS-539): the entry point clears the
            # leftover group, so the assignee is org-binding-validated only —
            # NOT an exact review-lane member. This is exactly how rei's
            # capability-matched assignments looked when the sweep ate them.
            assignment_group=None,
            assignee_id=4242,
            assignee_email='rei@shizuha.com',
            workflow_name='reviewed-task',
            project=self.project,
            metadata={
                'direct_assignment': {
                    'assignee_email': 'rei@shizuha.com',
                    'reason': 'capability-matched review (has Origin access)',
                    'by': 'review-seat',
                },
            },
        )
        unpinned = Item.objects.create(
            title='leftover prior-stage owner, no pin',
            mode='task',
            status='in_review',
            priority='high',
            assignment_group=None,
            assignee_id=4243,
            assignee_email='rui@shizuha.com',
            workflow_name='reviewed-task',
            project=self.project,
        )

        result = _reclaim_wip_overload()

        pinned.refresh_from_db()
        unpinned.refresh_from_db()
        # Pinned item: untouched — assignment and status intact, no sweep
        # comment, not counted as reclaimed.
        self.assertEqual(pinned.status, 'in_review')
        self.assertIsNone(pinned.assignment_group)
        self.assertEqual(pinned.assignee_email, 'rei@shizuha.com')
        self.assertEqual(pinned.assignee_id, 4242)
        self.assertFalse(
            pinned.comments.filter(
                content__contains='Removed stale prior-stage owner',
            ).exists(),
        )
        # Unpinned twin: swept back to the lane queue as before.
        self.assertEqual(result['active_queued'], 1)
        self.assertIsNone(unpinned.assignee_email)
        self.assertIsNone(unpinned.assignee_id)
        self.assertTrue(
            unpinned.comments.filter(
                content__contains='Removed stale prior-stage owner',
            ).exists(),
        )

    def test_recovery_preserves_typed_qa_lane_across_repeated_reconciliation(self):
        """Production order: Review bails to QA, recovery x4, QA closes."""
        from tasks.models import (
            Item, Status, TeamRoster, Workflow, WorkflowStatus,
            WorkflowTransition,
        )

        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='qa',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['qa-one@example.com', 'qa-two@example.com']],
                'is_active': True,
                'wip_enforced': True,
                'ready_wip_cap': 5,
                'active_wip_cap': 5,
            },
        )
        in_review, _ = Status.objects.get_or_create(
            slug='in_review',
            defaults={'name': 'In Review', 'category': 'in_progress'},
        )
        done, _ = Status.objects.get_or_create(
            slug='done',
            defaults={'name': 'Done', 'category': 'done'},
        )
        workflow = Workflow.objects.create(
            name='PLS-581 post-merge QA sequence',
            slug='pls-581-post-merge-qa-sequence',
            initial_status=in_review,
            is_active=True,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=in_review, position=0,
        )
        WorkflowStatus.objects.create(
            workflow=workflow, status=done, position=1,
        )
        close = WorkflowTransition.objects.create(
            workflow=workflow,
            name='QA Verification Passed',
            from_status='in_review',
            to_status='done',
            position=0,
        )
        item = Item.objects.create(
            title='post-merge operational verification',
            mode='task',
            status='in_review',
            priority='urgent',
            assignment_group='qa',
            workflow_name=workflow.slug,
            project=self.project,
            labels=['no-pr'],
            metadata={'execution_evidence_complete': True},
        )
        # The transition already routed this item to a valid QA seat.  The old
        # status-only repair still replaced this authoritative lane with Review.
        Item.objects.filter(pk=item.pk).update(
            assignee_id=9600,
            assignee_email='qa-one@example.com',
        )
        item.refresh_from_db()

        # Model repeated cleanup/capacity/failover reconciliation while the
        # concrete seat rotates only within the authoritative QA roster.
        for index, email in enumerate(('qa-one@example.com', 'qa-two@example.com',
                                       'qa-one@example.com', 'qa-two@example.com')):
            Item.objects.filter(pk=item.pk).update(
                assignee_id=9600 + index,
                assignee_email=email,
            )
            item.refresh_from_db()
            _reclaim_wip_overload()
            item.refresh_from_db()
            self.assertEqual(item.status, 'in_review')
            self.assertEqual(item.assignment_group, 'qa')
            self.assertNotEqual(item.assignment_group, 'review')

        WorkflowService.execute_transition(
            item,
            'done',
            transition_id=str(close.id),
        )
        item.refresh_from_db()
        self.assertEqual(item.status, 'done')

    def test_recovery_repairs_targeted_code_review_from_stale_engineering_lane(self):
        from tasks.models import Item, TeamRoster

        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.EMAIL]],
                'is_active': True,
                'wip_enforced': True,
                'ready_wip_cap': 5,
                'active_wip_cap': 5,
            },
        )

        item = Item.objects.create(
            title='targeted code review',
            mode='task',
            status='in_review',
            priority='urgent',
            assignment_group='engineering',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
            metadata={
                'pull_requests': [{
                    'url': 'https://origin.shizuha.com/shizuha-labs/pulse/pulls/581',
                    'repo': 'shizuha-labs/pulse',
                    'number': 581,
                    'head_sha': 'a' * 40,
                    'status': 'open',
                }],
            },
        )
        result = _reclaim_wip_overload()

        item.refresh_from_db()
        self.assertEqual(result['active_queued'], 1)
        self.assertEqual(item.assignment_group, 'review')
        self.assertIsNone(item.assignee_email)
        self.assertIsNone(item.assignee_id)

    def test_recovery_respects_forward_merge_handoff_despite_linked_pr(self):
        """CON-8745: APPROVED review + team=merge + linked unmerged PR → untouched.

        A merge-pending item always carries a linked unmerged PR; the old
        linked-PR provenance branch returned Review before consulting the team
        field, so the stale-owner repair bounced explicit merge handoffs back
        to the review queue (PLAT-8519: nine capacity pulls in one day after
        the formal APPROVED, plus PLS-1238 bounces of the direct override).
        Forward handoffs are authoritative: the merge-lane owner belongs to
        the merge roster, so the item is healthy exactly where it is.
        """
        from tasks.models import Item, TeamRoster

        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='merge',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.EMAIL]],
                'is_active': True,
                'wip_enforced': True,
                'ready_wip_cap': 5,
                'active_wip_cap': 5,
            },
        )

        item = Item.objects.create(
            title='review-complete merge pending',
            mode='task',
            status='in_review',
            priority='urgent',
            assignment_group='merge',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-bug',
            project=self.project,
            metadata={
                'pull_requests': [{
                    'url': 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/1113',
                    'repo': 'shizuha-labs/deploy',
                    'number': 1113,
                    'head_sha': 'b' * 40,
                    'status': 'open',
                }],
            },
        )
        result = _reclaim_wip_overload()

        item.refresh_from_db()
        self.assertEqual(result['active_queued'], 0)
        self.assertEqual(item.status, 'in_review')
        self.assertEqual(item.assignment_group, 'merge')
        self.assertEqual(item.assignee_email, self.EMAIL)
        self.assertFalse(
            item.comments.filter(
                content__contains='Removed stale prior-stage owner',
            ).exists(),
        )

    def test_recovery_respects_forward_qa_handoff_despite_linked_pr(self):
        """CON-8745: QA handoff + linked unmerged PR → untouched.

        Same forward-handoff rule for the QA lane: an ``in_review`` item
        handed to QA for post-merge operational verification keeps its typed
        lane even though a linked PR is present (the PR heuristic exists for
        stale upstream fields, not forward handoffs).
        """
        from tasks.models import Item, TeamRoster

        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='qa',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.EMAIL]],
                'is_active': True,
                'wip_enforced': True,
                'ready_wip_cap': 5,
                'active_wip_cap': 5,
            },
        )

        item = Item.objects.create(
            title='post-merge QA verification with linked fix PR',
            mode='task',
            status='in_review',
            priority='urgent',
            assignment_group='qa',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-bug',
            project=self.project,
            metadata={
                'pull_requests': [{
                    'url': 'https://origin.shizuha.com/shizuha-labs/deploy/pulls/1110',
                    'repo': 'shizuha-labs/deploy',
                    'number': 1110,
                    'head_sha': 'c' * 40,
                    'status': 'open',
                }],
            },
        )
        result = _reclaim_wip_overload()

        item.refresh_from_db()
        self.assertEqual(result['active_queued'], 0)
        self.assertEqual(item.status, 'in_review')
        self.assertEqual(item.assignment_group, 'qa')
        self.assertEqual(item.assignee_email, self.EMAIL)
        self.assertFalse(
            item.comments.filter(
                content__contains='Removed stale prior-stage owner',
            ).exists(),
        )

    def test_recovery_rechecks_lane_after_concurrent_review_to_qa_reroute(self):
        """Unlocked Review snapshot must not overwrite a locked QA correction."""
        from django.db.models.query import QuerySet
        from tasks.models import Item, TeamRoster

        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='review',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['reviewer@example.com']],
                'is_active': True,
                'wip_enforced': True,
                'ready_wip_cap': 5,
                'active_wip_cap': 5,
            },
        )
        TeamRoster.objects.update_or_create(
            organization_id=1, team_name='qa',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['qa-one@example.com']],
                'is_active': True,
                'wip_enforced': True,
                'ready_wip_cap': 5,
                'active_wip_cap': 5,
            },
        )
        item = Item.objects.create(
            title='review scope bail races stale-owner recovery',
            mode='task',
            status='in_review',
            priority='urgent',
            assignment_group='review',
            assignee_id=9600,
            assignee_email='reviewer@example.com',
            workflow_name='change-request',
            project=self.project,
            labels=['no-pr'],
        )
        # Make the pre-lock Review owner stale without manufacturing an invalid
        # assignment row (the database guard correctly rejects those writes).
        TeamRoster.objects.filter(
            organization_id=1,
            team_name='review',
        ).update(
            tiers=[['other-reviewer@example.com']],
        )

        original_select_for_update = QuerySet.select_for_update
        correction_injected = False

        def inject_qa_correction(queryset, *args, **kwargs):
            nonlocal correction_injected
            if queryset.model is Item and not correction_injected:
                correction_injected = True
                Item.objects.filter(pk=item.pk).update(
                    assignment_group='qa',
                    assignee_id=9600,
                    assignee_email='qa-one@example.com',
                )
            return original_select_for_update(queryset, *args, **kwargs)

        with patch.object(
            QuerySet,
            'select_for_update',
            autospec=True,
            side_effect=inject_qa_correction,
        ):
            result = _reclaim_wip_overload()

        item.refresh_from_db()
        self.assertTrue(correction_injected)
        self.assertEqual(result['active_queued'], 0)
        self.assertEqual(item.status, 'in_review')
        self.assertEqual(item.assignment_group, 'qa')
        self.assertEqual(item.assignee_email, 'qa-one@example.com')

    @patch.dict(os.environ, {
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_API_HEALTH_URL': 'disabled',
        # Review is NOT on the legacy env list — roster wip_enforced is SoT.
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
        'PULSE_WIP_CAP': '5',
        'PULSE_INFLIGHT_CAP': '15',
    })
    def test_pls667_ready_cap_reclaim_not_undone_by_same_or_next_capacity_pull(self):
        """PLS-667: ready-cap recovery must converge; capacity-pull must not refill.

        Production order (BKSD-37 / QNT-33):
          1. Owner is above assigned-ready cap but under active/in-flight caps.
          2. ``_reclaim_wip_overload`` returns excess ready work to the team queue.
          3. Later PLAT-236 capacity-pull in the SAME ``sweep_rebalance_pods``
             (and the next cadence) must NOT re-assign that work to the same
             seat while ready-cap is still full.
        Two consecutive real sweeps must be a fixed point: no duplicate
        pull/return comments, overflow stays group-queued.
        """
        from tasks.models import Comment, Item, TeamRoster

        TeamRoster.objects.filter(
            organization_id=1, team_name='review',
        ).update(
            wip_enforced=True,
            active_wip_cap=5,
            ready_wip_cap=1,
            tiers=[[self.EMAIL]],
            junior_emails=[self.EMAIL],
        )
        WorkflowService._invalidate_team_roster_caches()

        kept = Item.objects.create(
            title='kept ready item',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='review',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )
        overflow = Item.objects.create(
            title='overflow ready item',
            mode='task',
            status='open',
            priority='high',
            assignment_group='review',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )
        # Active headroom must not authorize a ready-slot refill.
        active = Item.objects.create(
            title='active hands-on (does not fill ready cap)',
            mode='task',
            status='in_progress',
            priority='normal',
            assignment_group='review',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )

        def _snapshot():
            for item in (kept, overflow, active):
                item.refresh_from_db()
            return {
                'kept': (kept.assignee_email, kept.assignment_group),
                'overflow': (overflow.assignee_email, overflow.assignment_group),
                'active': (active.assignee_email, active.assignment_group),
            }

        with patch('tasks.user_service.user_service', self._user_service()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            first = sweep_rebalance_pods.run()
            snap1 = _snapshot()
            second = sweep_rebalance_pods.run()
            snap2 = _snapshot()

        self.assertEqual(snap1['kept'][0], self.EMAIL)
        self.assertIsNone(snap1['overflow'][0], snap1)
        self.assertEqual(snap1['overflow'][1], 'review')
        self.assertEqual(snap1['active'][0], self.EMAIL)
        self.assertEqual(snap2, snap1)
        pull_comments = Comment.objects.filter(
            item=overflow,
            author_email='system@pulse',
            content__icontains='Pulled from the',
        ).count()
        self.assertEqual(
            pull_comments, 0,
            'capacity-pull must not re-assign ready-cap overflow (PLS-667)',
        )
        return_comments = Comment.objects.filter(
            item=overflow,
            author_email='system@pulse',
            content__icontains='assigned-ready cap',
        ).count()
        self.assertLessEqual(return_comments, 1)
        self.assertIsInstance(first.get('moves'), int)
        self.assertIsInstance(second.get('moves'), int)


_UIDS = {'ryo@shizuha.com': 1, 'jun@shizuha.com': 2, 'kai@shizuha.com': 3, 'sara@shizuha.com': 4}


# These cases are written against ACTIVE_CAP=5 / INFLIGHT_CAP=10; pin them so the
# scenarios stay valid regardless of the shipped defaults (raised to 10/15 under
# the 2026-06-26 capacity lift).
@patch.dict(os.environ, {'PULSE_WIP_CAP': '5', 'PULSE_INFLIGHT_CAP': '10'})
class TwoTierResolverEligibilityTests(SimpleTestCase):
    """`_resolve_team_assignee(enforce_cap=True)` honors the two-tier predicate:
    a member with only review-pipeline work is eligible; all-full → group-queued."""

    def setUp(self):
        us = patch('tasks.user_service.user_service').start()
        self.addCleanup(patch.stopall)
        us.get_user_by_email.side_effect = lambda e: ({'id': _UIDS[e]} if e in _UIDS else None)
        patch.object(WorkflowService, '_is_agent_available_for_assignment',
                     side_effect=lambda e: e in _UIDS).start()
        # PLAT-431: the engineering roster now lives only in the DB (TeamRoster);
        # a SimpleTestCase has no DB, so _pod_roster() returns {} and the resolver
        # falls back to the default lead. Patch it to the 4 seeded members so the
        # cap/least-loaded selection under test is exercised.
        patch.object(WorkflowService, '_pod_roster',
                     return_value={'engineering': list(_UIDS.keys())}).start()
        # Per-uid (active, inflight) loads, controllable per test.
        self.active = {1: 5, 2: 5, 3: 5, 4: 0}
        self.inflight = {1: 5, 2: 5, 3: 5, 4: 4}
        patch.object(WorkflowService, '_agent_active_load',
                     side_effect=lambda uid, email=None: self.active.get(int(uid), 0)).start()
        patch.object(WorkflowService, '_agent_workable_load',
                     side_effect=lambda uid, email=None: self.inflight.get(int(uid), 0)).start()

    def _resolve(self):
        # No project_key → skip the stage-lead pin; exercise the cap filter +
        # least-loaded selection directly.
        return WorkflowService._resolve_team_assignee(
            'engineering', exclude_email=None, enforce_cap=True,
        )

    def test_review_pipeline_member_is_pulled(self):
        # ryo/jun/kai full at active cap 5; sara = 0 active + 4 in_review → the
        # only eligible member → new work routes to sara (was the starvation bug).
        uid, email = self._resolve()
        self.assertEqual((uid, email), (4, 'sara@shizuha.com'))

    def test_all_full_leaves_group_queued(self):
        # Everyone at active cap OR inflight bound → (None, None) → stays queued.
        self.active = {1: 5, 2: 5, 3: 5, 4: 4}
        self.inflight = {1: 5, 2: 5, 3: 5, 4: 10}  # sara now at runaway bound
        self.assertEqual(self._resolve(), (None, None))

    def test_routes_to_least_active(self):
        # All eligible; route to least HANDS-ON load (jun=1), not least inflight.
        self.active = {1: 3, 2: 1, 3: 2, 4: 4}
        self.inflight = {1: 3, 2: 9, 3: 2, 4: 4}
        uid, email = self._resolve()
        self.assertEqual((uid, email), (2, 'jun@shizuha.com'))

    def test_non_cap_review_team_routes_by_pipeline_load(self):
        # Review work lives in pipeline statuses, so non-cap teams must not use
        # active-first ranking. ryo has fewer active tasks but much more total
        # in-flight review work; jun should receive the next review item.
        self.active = {1: 1, 2: 2}
        self.inflight = {1: 20, 2: 2}
        with patch.object(WorkflowService, '_pod_roster',
                          return_value={'review': ['ryo@shizuha.com', 'jun@shizuha.com']}):
            uid, email = WorkflowService._resolve_team_assignee(
                'review', exclude_email=None, enforce_cap=False,
            )
        self.assertEqual((uid, email), (2, 'jun@shizuha.com'))


class CapacityPullRoleGatingConstantTests(SimpleTestCase):
    """PLAT-534: capacity-pull must gate on team AND status-role.

    Pure constant/unit tests — no DB required. Verify the exclusion set is
    defined correctly and that devops/review teams are not over-restricted.
    """

    # The full set engineering must never pull (devops + review-pipeline gated).
    _ENG_EXCLUDED = {
        'awaiting_deploy', 'awaiting_verification', 'verification',
        'in_review', 'in_qa', 'awaiting_merge', 'applied',
    }

    def test_engineering_exclusion_set_is_correct(self):
        excluded = WorkflowService._CAPACITY_PULL_EXCLUDED_STATUSES_BY_TEAM.get('engineering', set())
        self.assertSetEqual(excluded, self._ENG_EXCLUDED)

    def test_engineering_excludes_awaiting_deploy(self):
        excluded = WorkflowService._CAPACITY_PULL_EXCLUDED_STATUSES_BY_TEAM.get('engineering', set())
        self.assertIn('awaiting_deploy', excluded)

    def test_engineering_excludes_in_review(self):
        excluded = WorkflowService._CAPACITY_PULL_EXCLUDED_STATUSES_BY_TEAM.get('engineering', set())
        self.assertIn('in_review', excluded)

    def test_engineering_excludes_awaiting_merge(self):
        excluded = WorkflowService._CAPACITY_PULL_EXCLUDED_STATUSES_BY_TEAM.get('engineering', set())
        self.assertIn('awaiting_merge', excluded)

    def test_engineering_excludes_verification(self):
        excluded = WorkflowService._CAPACITY_PULL_EXCLUDED_STATUSES_BY_TEAM.get('engineering', set())
        self.assertIn('verification', excluded)

    def test_devops_may_pull_awaiting_deploy(self):
        """devops team's own capacity queue CAN contain awaiting_deploy tasks."""
        excluded = WorkflowService._CAPACITY_PULL_EXCLUDED_STATUSES_BY_TEAM.get('devops', set())
        self.assertNotIn('awaiting_deploy', excluded)

    def test_review_team_may_pull_in_review(self):
        """review team's own capacity queue CAN contain in_review tasks."""
        excluded = WorkflowService._CAPACITY_PULL_EXCLUDED_STATUSES_BY_TEAM.get('review', set())
        self.assertNotIn('in_review', excluded)


class CapacityPullRoleGatingDBTests(TestCase):
    """PLAT-534: DB-backed regression — _queued_tasks_for_team('engineering')
    must never return tasks in devops/review-gated statuses even when those
    tasks have assignment_group='engineering' and no concrete assignee."""

    def _make_item(self, status, assignment_group='engineering'):
        from tasks.models import Item
        # Pin a workflow that actually contains these pipeline statuses. Without
        # it, a task Item auto-attaches the `simple` workflow and Item.save()
        # coerces the gated status away (awaiting_deploy/awaiting_merge/
        # verification→todo, in_review→in_progress), so the row would no longer
        # be in the status the gating test means to exercise.
        return Item.objects.create(
            title=f'test-{status}',
            mode='task',
            status=status,
            assignment_group=assignment_group,
            workflow_name='autonomous-dev',
        )

    def test_awaiting_deploy_not_pulled_into_engineering(self):
        self._make_item('awaiting_deploy')
        result = WorkflowService._queued_tasks_for_team('engineering')
        self.assertEqual(result, [],
                         "awaiting_deploy task must not appear in the engineering capacity queue")

    def test_in_review_not_pulled_into_engineering(self):
        self._make_item('in_review')
        result = WorkflowService._queued_tasks_for_team('engineering')
        self.assertEqual(result, [],
                         "in_review task must not appear in the engineering capacity queue")

    def test_awaiting_merge_not_pulled_into_engineering(self):
        self._make_item('awaiting_merge')
        result = WorkflowService._queued_tasks_for_team('engineering')
        self.assertEqual(result, [],
                         "awaiting_merge task must not appear in the engineering capacity queue")

    def test_verification_not_pulled_into_engineering(self):
        self._make_item('verification')
        result = WorkflowService._queued_tasks_for_team('engineering')
        self.assertEqual(result, [],
                         "verification task must not appear in the engineering capacity queue")

    def test_open_task_is_pulled_into_engineering(self):
        """Baseline: a valid open engineering task IS returned."""
        item = self._make_item('open')
        result = WorkflowService._queued_tasks_for_team('engineering')
        self.assertEqual([it.pk for it in result], [item.pk])

    def test_root_blocker_inherits_urgent_priority_and_cycle_terminates(self):
        """Admission mirrors production queue order for an ultimate blocker.

        The runnable root releases an active blocked descendant, so it must
        outrank unrelated high work.  Operator directive 2026-08-08: a task
        that blocks active work is escalated to STORED urgent priority (not
        just a derived queue rank) so the rebalance sweep's stored-priority
        filter assigns it before any lower-priority work.  The descendant graph
        contains a deliberately malformed cycle (inserted without signals) to
        prove the recursive rank remains bounded on legacy/corrupt link data.
        """
        from tasks.models import ItemLink

        root = self._make_item('open')
        root.title = 'normal root releasing urgent work'
        root.priority = 'normal'
        root.save(update_fields=['title', 'priority'])

        unrelated_high = self._make_item('open')
        unrelated_high.title = 'unrelated high work'
        unrelated_high.priority = 'high'
        unrelated_high.save(update_fields=['title', 'priority'])

        urgent_descendant = self._make_item('open')
        urgent_descendant.priority = 'urgent'
        urgent_descendant.save(update_fields=['priority'])
        root_link = ItemLink(
            source_item=root,
            target_item=urgent_descendant,
            link_type='blocks',
            created_by_id=1,
            created_by_email='queue-test@shizuha.com',
        )
        # Build the same valid graph order as a production transition: durable
        # owned link first, then blocked status.  Isolated tests do not seed the
        # workflow transition table used by auto_block_from_link.
        root_link._skip_auto_block = True
        root_link.save()
        urgent_descendant.status = 'blocked'
        urgent_descendant.save(update_fields=['status'])
        urgent_descendant.refresh_from_db()
        self.assertEqual(urgent_descendant.status, 'blocked')

        second_descendant = self._make_item('open')
        descendant_link = ItemLink(
            source_item=urgent_descendant,
            target_item=second_descendant,
            link_type='blocks',
            created_by_id=1,
            created_by_email='queue-test@shizuha.com',
        )
        descendant_link._skip_auto_block = True
        descendant_link.save()
        second_descendant.status = 'blocked'
        second_descendant.save(update_fields=['status'])
        second_descendant.refresh_from_db()
        self.assertEqual(second_descendant.status, 'blocked')

        # Inverse orientation means second_descendant blocks urgent_descendant,
        # closing a descendant-only cycle without invoking the API cycle guard.
        ItemLink.objects.bulk_create([ItemLink(
            source_item=urgent_descendant,
            target_item=second_descendant,
            link_type='is_blocked_by',
            created_by_id=1,
            created_by_email='queue-test@shizuha.com',
        )])

        result = WorkflowService._queued_tasks_for_team('engineering')
        self.assertEqual(
            [item.pk for item in result[:2]],
            [root.pk, unrelated_high.pk],
        )
        # Operator directive 2026-08-08: blocker escalation is persisted to the
        # stored priority (not merely a derived execution rank) so the rebalance
        # sweep assigns the blocker before lower-priority work.
        self.assertEqual(root.priority, 'urgent', 'blocker priority must be escalated')

    def test_capacity_queue_uses_stage_then_fifo_inside_priority(self):
        open_oldest = self._make_item('open', assignment_group='review')
        open_oldest.priority = 'high'
        open_oldest.save(update_fields=['priority'])
        review_oldest = self._make_item('in_review', assignment_group='review')
        review_oldest.priority = 'high'
        review_oldest.save(update_fields=['priority'])
        review_newest = self._make_item('in_review', assignment_group='review')
        review_newest.priority = 'high'
        review_newest.save(update_fields=['priority'])

        result = WorkflowService._queued_tasks_for_team('review')
        self.assertEqual(
            [item.pk for item in result],
            [review_oldest.pk, review_newest.pk, open_oldest.pk],
        )

    @patch.dict(os.environ, {
        'PULSE_WIP_CAP': '1',
        'PULSE_INFLIGHT_CAP': '10',
        'PULSE_WIP_CAP_TEAMS': 'engineering',
    })
    def test_urgent_changes_order_but_does_not_bypass_capacity(self):
        from tasks.models import TeamRoster

        email = 'full-engineer@shizuha.com'
        urgent = self._make_item('open')
        urgent.priority = 'urgent'
        urgent.save(update_fields=['priority'])
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[email]],
                'is_active': True,
            },
        )

        with patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True), \
             patch.object(WorkflowService, '_agent_active_load', return_value=1), \
             patch.object(WorkflowService, '_agent_workable_load', return_value=1):
            pulled = WorkflowService._pull_next_queued_for(991, email)

        urgent.refresh_from_db()
        self.assertIsNone(pulled)
        self.assertIsNone(urgent.assignee_id)
        self.assertEqual(
            WorkflowService._queued_tasks_for_team('engineering')[0].pk,
            urgent.pk,
        )


class RebalanceQueueDrainTests(TestCase):
    """PLAT-867: the rebalance backstop drains group-only queues for non-cap teams.

    DevOps/review/merge are intentionally not WIP-cap-enforced by default, but
    stale-alert/fallback rows can still land with assignment_group set and no
    concrete assignee. Those rows must be pulled to the lightest active roster
    member rather than waiting forever for a cap-pull hook that never runs.
    """

    @patch.dict(os.environ, {
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
    })
    def test_non_cap_team_queue_is_drained_to_active_member(self):
        from tasks.models import Item, Project, TeamRoster

        project = Project.objects.create(
            org_id=1,
            name='Rebalance Queue Drain',
            key='RBQD',
        )
        queued = Item.objects.create(
            title='devops queued alert',
            mode='task',
            status='open',
            priority='high',
            assignment_group='devops',
            workflow_name='simple',
            project=project,
        )
        # Simulate a row minted after the original rename by an old workflow
        # JSON post-function. New inserts canonicalize this spelling.
        Item.objects.filter(pk=queued.pk).update(
            assignment_group='platform-ops',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='devops',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['jun@shizuha.com']],
                'is_active': True,
            },
        )

        class FakeUserService:
            def get_user_by_email(self, email):
                return {'id': 105, 'email': email} if email == 'jun@shizuha.com' else None

            def get_user_by_id(self, user_id):
                return {'id': user_id, 'email': 'jun@shizuha.com'} if int(user_id) == 105 else None

        with patch('tasks.user_service.user_service', FakeUserService()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            result = sweep_rebalance_pods.run()

        queued.refresh_from_db()
        self.assertEqual(result['moves'], 1)
        self.assertEqual(queued.assignee_id, 105)
        self.assertEqual(queued.assignee_email, 'jun@shizuha.com')
        self.assertEqual(queued.assignment_group, 'devops')
        self.assertFalse(WorkflowService._cap_applies('devops'))

    @patch.dict(os.environ, {
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
    })
    def test_roster_wip_cap_prevents_queue_drain_from_refilling_released_work(self):
        """PLS-509 recovery and the same sweep's queue drain share one policy.

        Review is deliberately absent from the legacy environment cap list, but
        a live wip_enforced roster row is authoritative.  The drain must assign
        only the highest-priority ready item and leave the rest group-queued.
        """
        from tasks.models import Item, Project, TeamRoster

        project = Project.objects.create(
            org_id=1,
            name='Roster WIP Queue Drain',
            key='RWQD',
        )
        queued = [
            Item.objects.create(
                title=f'review queue item {priority}',
                mode='task',
                status='open',
                priority=priority,
                assignment_group='review',
                workflow_name='autonomous-dev',
                project=project,
            )
            for priority in ('normal', 'urgent', 'high')
        ]
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='review',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['revi@shizuha.com']],
                'is_active': True,
                'wip_enforced': True,
                'active_wip_cap': 1,
                'ready_wip_cap': 1,
            },
        )

        class FakeUserService:
            def get_user_by_email(self, email):
                return {'id': 509, 'email': email} if email == 'revi@shizuha.com' else None

            def get_user_by_id(self, user_id):
                return (
                    {'id': 509, 'email': 'revi@shizuha.com'}
                    if int(user_id) == 509 else None
                )

        with patch('tasks.user_service.user_service', FakeUserService()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            result = sweep_rebalance_pods.run()

        for item in queued:
            item.refresh_from_db()
        assigned = [item for item in queued if item.assignee_email]
        self.assertEqual(result['moves'], 1)
        self.assertEqual([item.priority for item in assigned], ['urgent'])
        self.assertEqual(assigned[0].assignee_email, 'revi@shizuha.com')
        self.assertEqual(
            sum(item.assignee_email is None for item in queued),
            2,
        )
        self.assertFalse(WorkflowService._cap_applies('review'))

    @patch.dict(os.environ, {
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
    })
    def test_queue_drain_isolates_one_failed_write_and_continues(self):
        from tasks.models import Item, Project, TeamRoster

        project = Project.objects.create(
            org_id=1,
            name='Rebalance Queue Write Isolation',
            key='RBQWI',
        )
        bad = Item.objects.create(
            title='bad legacy queue row',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='devops',
            workflow_name='simple',
            project=project,
        )
        good = Item.objects.create(
            title='later healthy queue row',
            mode='task',
            status='open',
            priority='high',
            assignment_group='devops',
            workflow_name='simple',
            project=project,
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='devops',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['jun@shizuha.com']],
                'is_active': True,
            },
        )

        class FakeUserService:
            def get_user_by_email(self, email):
                return {'id': 105, 'email': email} if email == 'jun@shizuha.com' else None

            def get_user_by_id(self, user_id):
                return {'id': user_id, 'email': 'jun@shizuha.com'} if int(user_id) == 105 else None

        real_save = Item.save

        def fail_one_assignment(item, *args, **kwargs):
            update_fields = set(kwargs.get('update_fields') or [])
            if item.pk == bad.pk and 'assignee_id' in update_fields:
                raise IntegrityError('simulated assignment write failure')
            return real_save(item, *args, **kwargs)

        with patch('tasks.user_service.user_service', FakeUserService()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True), \
             patch.object(Item, 'save', new=fail_one_assignment):
            result = sweep_rebalance_pods.run()

        bad.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(result['moves'], 1)
        self.assertIsNone(bad.assignee_id)
        self.assertFalse(bad.assignee_email)
        self.assertEqual(good.assignee_id, 105)
        self.assertEqual(good.assignee_email, 'jun@shizuha.com')

    @patch.dict(os.environ, {
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
    })
    def test_reviewer_holding_one_review_still_pulls_next_queued_review(self):
        """Regression: a live reviewer at active_wip_cap=1 holding one in_review
        item must STILL be assigned the next group-queued review.

        PLAT-390 two-tier: review-pipeline work is in-flight, not hands-on, so
        it consumes the READY capacity dimension (ready_wip_cap=5), not the
        active dimension (active_wip_cap=1). Before the fix, in_review mapped to
        an ACTIVE slot and the queue-drain marked every live reviewer
        ``over_capacity`` at a single review — the exact group-queued review
        starvation this pins.
        """
        from tasks.models import Item, Project, TeamRoster

        project = Project.objects.create(
            org_id=1,
            name='Reviewer Pull Regression',
            key='RVPL',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='review',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['rei@shizuha.com']],
                'is_active': True,
                'wip_enforced': True,
                'active_wip_cap': 1,
                'ready_wip_cap': 5,
            },
        )
        # The reviewer already holds one in_review item (their "active" seat).
        Item.objects.create(
            title='held review', mode='task', status='in_review',
            priority='high', assignment_group='review',
            assignee_id=106, assignee_email='rei@shizuha.com',
            workflow_name='autonomous-dev', project=project,
        )
        # A group-queued in_review item awaiting a concrete reviewer.
        queued = Item.objects.create(
            title='next queued review', mode='task', status='in_review',
            priority='urgent', assignment_group='review',
            workflow_name='autonomous-dev', project=project,
        )

        class FakeUserService:
            def get_user_by_email(self, email):
                return {'id': 106, 'email': email} if email == 'rei@shizuha.com' else None

            def get_user_by_id(self, user_id):
                return {'id': user_id, 'email': 'rei@shizuha.com'} if int(user_id) == 106 else None

        with patch('tasks.user_service.user_service', FakeUserService()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            result = sweep_rebalance_pods.run()

        queued.refresh_from_db()
        self.assertEqual(result['moves'], 1)
        self.assertEqual(queued.assignee_id, 106)
        self.assertEqual(queued.assignee_email, 'rei@shizuha.com')
        self.assertEqual(queued.assignment_group, 'review')


class RebalanceHealthAndBoundaryTests(TestCase):
    """PLAT-916: rebalance must skip on Pulse API outage and never infer
    ownership for NULL/blank assignment_group rows."""

    def _fake_user_service(self, users):
        class FakeUserService:
            def get_user_by_email(self, email):
                rec = users.get((email or '').strip().lower())
                return {'id': rec[0], 'email': rec[1]} if rec else None

            def get_user_by_id(self, user_id):
                for uid, email in users.values():
                    if int(user_id) == uid:
                        return {'id': uid, 'email': email}
                return None
        return FakeUserService()

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'http://pulse.invalid/api/health/',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
    })
    def test_pulse_api_health_failure_skips_entire_rebalance(self):
        from urllib.error import URLError
        from tasks.models import Item

        queued = Item.objects.create(
            title='queued engineering task',
            mode='task',
            status='open',
            priority='high',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
        )

        with patch('urllib.request.urlopen', side_effect=URLError('pulse down')), \
             patch('tasks.user_service.user_service', self._fake_user_service({
                 'kai@shizuha.com': (101, 'kai@shizuha.com'),
             })), \
             patch.object(WorkflowService, '_pod_roster', return_value={'engineering': ['kai@shizuha.com']}), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            result = sweep_rebalance_pods.run()

        queued.refresh_from_db()
        self.assertEqual(result, {'moves': 0, 'pods': {}, 'skipped': 'api_unreachable'})
        self.assertIsNone(queued.assignee_id)
        self.assertIsNone(queued.assignee_email)

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'http://pulse.invalid/api/health/',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
    })
    def test_pulse_api_health_500_skips_entire_rebalance(self):
        """PLAT-930: an HTTP 5xx from the Pulse API health endpoint is treated as
        degraded — the sweep must abort with skipped='api_degraded', not move any
        tasks.  Complements the unreachable case (test above) which only covers
        network-unreachable; 500 requires the probe to complete but return bad status.
        The health probe now uses ``requests.get`` (returns a ``status_code``)."""
        from unittest.mock import MagicMock
        from tasks.models import Item

        queued = Item.objects.create(
            title='queued engineering task should stay put on 500',
            mode='task',
            status='open',
            priority='high',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = 'internal server error'

        with patch('requests.get', return_value=mock_resp), \
             patch('tasks.user_service.user_service', self._fake_user_service({
                 'kai@shizuha.com': (101, 'kai@shizuha.com'),
             })), \
             patch.object(WorkflowService, '_pod_roster', return_value={'engineering': ['kai@shizuha.com']}), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            result = sweep_rebalance_pods.run()

        queued.refresh_from_db()
        self.assertEqual(result, {'moves': 0, 'pods': {}, 'skipped': 'api_degraded'})
        self.assertIsNone(queued.assignee_id)
        self.assertIsNone(queued.assignee_email)

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
    })
    def test_overloaded_task_redistributes_only_within_owning_team(self):
        """PLAT-930: when a member has more than max_skew tasks, the sweep must
        only move work to another member of the SAME team.  It must never touch an
        architecture (or any other pod's) task even if the assignment_group is wrong
        or another team happens to have spare capacity."""
        from tasks.models import Item, Project, TeamRoster

        project = Project.objects.create(
            org_id=1,
            name='PLAT-930 Team Boundaries',
            key='P930',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['ryo@shizuha.com', 'kai@shizuha.com']],
                'is_active': True,
            },
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='architecture',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['aoi@shizuha.com']],
                'is_active': True,
            },
        )

        # An engineering task owned by an overloaded member.
        eng_task = Item.objects.create(
            title='overloaded engineering task — should only go to eng peer',
            mode='task',
            status='open',
            priority='high',
            assignment_group='engineering',
            assignee_id=201,
            assignee_email='ryo@shizuha.com',
            workflow_name='autonomous-dev',
            project=project,
        )
        # A task that looks like it could absorb work but belongs to architecture.
        arch_task = Item.objects.create(
            title='architecture task — must never receive cross-team reassignment',
            mode='task',
            status='open',
            priority='high',
            assignment_group='architecture',
            assignee_id=202,
            assignee_email='aoi@shizuha.com',
            workflow_name='autonomous-dev',
            project=project,
        )

        users = {
            'ryo@shizuha.com': (201, 'ryo@shizuha.com'),
            'kai@shizuha.com': (202, 'kai@shizuha.com'),
            'aoi@shizuha.com': (203, 'aoi@shizuha.com'),
        }

        # Engineering roster: ryo (overloaded), kai (empty).
        # Architecture roster: aoi.  Sweep must move eng_task to kai only.
        with patch('tasks.user_service.user_service', self._fake_user_service(users)), \
             patch.object(WorkflowService, '_pod_roster', return_value={
                 'engineering': ['ryo@shizuha.com', 'kai@shizuha.com'],
                 'architecture': ['aoi@shizuha.com'],
             }), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            result = sweep_rebalance_pods.run(max_skew=0)

        eng_task.refresh_from_db()
        arch_task.refresh_from_db()

        # Engineering task must move to the under-loaded engineering peer.
        self.assertEqual(eng_task.assignee_email, 'kai@shizuha.com',
                         "engineering task must move to an engineering peer, not cross-team")
        # Architecture task must be untouched.
        self.assertEqual(arch_task.assignee_email, 'aoi@shizuha.com',
                         "architecture task must not be touched by the engineering pod sweep")

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
    })
    def test_no_available_same_team_assignee_leaves_task_in_place(self):
        """PLAT-930: when every member of the owning team is unavailable, the sweep
        must leave the task with its current assignee rather than routing it to a
        different team.  'Honest' means assignee stays put — not a silent cross-team
        move, and not a blank/null wipe."""
        from tasks.models import Item, Project

        _set_shizuha_rosters({
            'engineering': ['ryo@shizuha.com', 'kai@shizuha.com'],
        })
        project = Project.objects.create(
            name='Dark team rebalance test',
            key='RBHD',
            org_id=1,
        )
        stuck = Item.objects.create(
            project=project,
            title='task whose whole eng team is down — must stay in place',
            mode='task',
            status='open',
            priority='high',
            assignment_group='engineering',
            assignee_id=301,
            assignee_email='ryo@shizuha.com',
            workflow_name='autonomous-dev',
        )

        users = {
            'ryo@shizuha.com': (301, 'ryo@shizuha.com'),
            'kai@shizuha.com': (302, 'kai@shizuha.com'),
        }

        # Both engineering members are unavailable.
        with patch('tasks.user_service.user_service', self._fake_user_service(users)), \
             patch.object(WorkflowService, '_pod_roster', return_value={
                 'engineering': ['ryo@shizuha.com', 'kai@shizuha.com'],
             }), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          return_value=False):
            result = sweep_rebalance_pods.run()

        stuck.refresh_from_db()
        # Whole pod dark → sweep skips → zero moves → task must be unchanged.
        self.assertEqual(result['moves'], 0)
        self.assertEqual(stuck.assignee_id, 301,
                         "task must not be reassigned when no same-team member is available")
        self.assertEqual(stuck.assignee_email, 'ryo@shizuha.com',
                         "task must stay with original assignee, not cross-routed")

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
    })
    def test_unavailable_same_tier_agent_full_drains_blocked_work(self):
        """Provider quota/offline availability must evacuate all non-terminal work.

        Regression for Claude quota exhaustion: Hive correctly shows Claude agents
        as unavailable, and Pulse must not leave blocked/review-pipeline work pinned
        to those unavailable owners until usage resets.
        """
        from tasks.models import Item, Project

        project = Project.objects.create(
            org_id=1,
            name='Unavailable Owner Drain',
            key='UOD',
        )
        _set_shizuha_rosters({
            'engineering': ['jun@shizuha.com', 'kai@shizuha.com'],
        })
        blocked = create_blocked_item(
            title='blocked work should move off unavailable owner',
            mode='task',
            status='blocked',
            priority='high',
            assignment_group='engineering',
            assignee_id=401,
            assignee_email='jun@shizuha.com',
            workflow_name='autonomous-dev',
            project=project,
        )
        satisfy_blocked_invariant(blocked)
        in_review = Item.objects.create(
            title='review work should move off unavailable owner',
            mode='task',
            status='in_review',
            priority='high',
            assignment_group='engineering',
            assignee_id=401,
            assignee_email='jun@shizuha.com',
            workflow_name='autonomous-dev',
            project=project,
        )

        users = {
            'jun@shizuha.com': (401, 'jun@shizuha.com'),
            'kai@shizuha.com': (402, 'kai@shizuha.com'),
        }

        with patch('tasks.user_service.user_service', self._fake_user_service(users)), \
             patch.object(WorkflowService, '_pod_roster', return_value={
                 'engineering': ['jun@shizuha.com', 'kai@shizuha.com'],
             }), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda email: (email or '').strip().lower() == 'kai@shizuha.com'):
            result = sweep_rebalance_pods.run()

        blocked.refresh_from_db()
        in_review.refresh_from_db()
        self.assertEqual(result['moves'], 2)
        # PLS-524 reconciles this with the PLS-204 drain contract: linked-
        # blocked work leaves the unavailable concrete owner, but does not issue
        # a new concrete lease for work nobody can advance.
        self.assertIsNone(blocked.assignee_email)
        self.assertIsNone(blocked.assignee_id)
        self.assertEqual(blocked.assignment_group, 'engineering')
        # PLS-581: ``in_review`` alone is not proof of a code-Review stage.
        # With no linked targeting triple and an authoritative Engineering
        # assignment group, failover stays inside that lane rather than
        # manufacturing a Review route from the shared status slug.
        self.assertEqual(in_review.assignee_email, 'kai@shizuha.com')
        self.assertEqual(in_review.assignee_id, 402)
        self.assertEqual(in_review.assignment_group, 'engineering')

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_WIP_CAP': '2',
        'PULSE_INFLIGHT_CAP': '2',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
    })
    def test_unavailable_owner_drains_to_alive_peer_even_when_peer_at_cap(self):
        """Owned work beats cap protection: do not queue work on dead assignees."""
        from tasks.models import Item, Project

        project = Project.objects.create(
            org_id=1,
            name='At-cap Owner Drain',
            key='ACOD',
        )
        _set_shizuha_rosters({
            'engineering': ['jun@shizuha.com', 'kai@shizuha.com'],
        })
        stuck = Item.objects.create(
            title='owned work should not stay on unavailable assignee',
            mode='task',
            status='open',
            priority='high',
            assignment_group='engineering',
            assignee_id=501,
            assignee_email='jun@shizuha.com',
            workflow_name='autonomous-dev',
            project=project,
        )
        for i in range(2):
            Item.objects.create(
                title=f'kai existing work {i}',
                mode='task',
                status='open',
                assignment_group='engineering',
                assignee_id=502,
                assignee_email='kai@shizuha.com',
                workflow_name='autonomous-dev',
                project=project,
            )

        users = {
            'jun@shizuha.com': (501, 'jun@shizuha.com'),
            'kai@shizuha.com': (502, 'kai@shizuha.com'),
        }

        with patch('tasks.user_service.user_service', self._fake_user_service(users)), \
             patch.object(WorkflowService, '_pod_roster', return_value={
                 'engineering': ['jun@shizuha.com', 'kai@shizuha.com'],
             }), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda email: (email or '').strip().lower() == 'kai@shizuha.com'):
            result = sweep_rebalance_pods.run()

        stuck.refresh_from_db()
        self.assertEqual(result['moves'], 1)
        self.assertEqual(stuck.assignee_email, 'kai@shizuha.com')
        self.assertEqual(stuck.assignee_id, 502)

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
    })
    def test_null_assignment_group_is_not_inferred_as_pod_owned_work(self):
        from tasks.models import Item

        orphan = Item.objects.create(
            title='unknown owner task must not be inferred into engineering',
            mode='task',
            status='open',
            priority='high',
            assignment_group=None,
            assignee_id=101,
            assignee_email='ryo@shizuha.com',
            workflow_name='autonomous-dev',
        )

        with patch('tasks.user_service.user_service', self._fake_user_service({
                 'ryo@shizuha.com': (101, 'ryo@shizuha.com'),
                 'kai@shizuha.com': (102, 'kai@shizuha.com'),
             })), \
             patch.object(WorkflowService, '_pod_roster', return_value={'engineering': ['ryo@shizuha.com', 'kai@shizuha.com']}), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment', return_value=True):
            result = sweep_rebalance_pods.run(max_skew=0)

        orphan.refresh_from_db()
        self.assertEqual(result['moves'], 0)
        self.assertEqual(orphan.assignee_email, 'ryo@shizuha.com')
        self.assertEqual(orphan.assignee_id, 101)


class Plat954DataPlaneHealthGateTests(TestCase):
    """PLAT-954: data-plane health gate hardens the rebalance sweep against
    outages where the Django process is up but the DB connection is severed.

    The sweep must skip (0 moves, skipped='api_degraded') when
    _rebalance_pulse_api_health_gate returns (False, 'api_degraded'), and it
    must never cross team boundaries regardless of apparent load distribution.
    """

    def _fake_user_service(self, users):
        class FakeUserService:
            def get_user_by_email(self, email):
                rec = users.get((email or '').strip().lower())
                return {'id': rec[0], 'email': rec[1]} if rec else None

            def get_user_by_id(self, user_id):
                for uid, email in users.values():
                    if int(user_id) == uid:
                        return {'id': uid, 'email': email}
                return None
        return FakeUserService()

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'http://pulse.invalid/api/health/',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
    })
    def test_degraded_data_plane_api_skips_all_assignments(self):
        """PLAT-954 AC1: when the data-plane health endpoint returns 503
        (data_plane=error), the sweep emits 0 moves and skipped='api_degraded'.
        No task must be reassigned regardless of apparent load imbalance."""
        from unittest.mock import MagicMock
        from tasks.models import Item

        # Create a task that would normally be eligible for reassignment.
        queued = Item.objects.create(
            title='engineering task — must not move on degraded data plane',
            mode='task',
            status='open',
            priority='high',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
        )

        # Simulate a 503 response from the data-plane health endpoint.
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 503
        mock_resp.read.return_value = b'{"status": "degraded", "data_plane": "error", "detail": "DB unavailable"}'

        with patch('urllib.request.urlopen', return_value=mock_resp), \
             patch('tasks.user_service.user_service', self._fake_user_service({
                 'kai@shizuha.com': (101, 'kai@shizuha.com'),
             })), \
             patch.object(WorkflowService, '_pod_roster',
                          return_value={'engineering': ['kai@shizuha.com']}), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          return_value=True):
            result = sweep_rebalance_pods.run()

        queued.refresh_from_db()
        self.assertEqual(result['moves'], 0,
                         "degraded data-plane must suppress all reassignments")
        self.assertIn(result.get('skipped'), ('api_degraded', 'api_unhealthy', 'api_unreachable'),
                      "sweep must report a degraded/unreachable skip reason")
        self.assertIsNone(queued.assignee_id,
                          "task must remain unassigned when data-plane is degraded")

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,analytics',
    })
    def test_team_boundary_invariant_engineering_analytics_never_cross_to_architecture(self):
        """PLAT-954 AC2: after a sweep run, tasks in engineering and analytics
        groups must never be reassigned to an architecture group member, even if
        architecture members appear to have lower load."""
        from tasks.models import Item, Project

        project = Project.objects.create(
            org_id=1,
            name='PLAT-954 Boundaries',
            key='P954',
        )
        _set_shizuha_rosters({
            'engineering': ['ryo@shizuha.com', 'kai@shizuha.com'],
            'research-analytics': ['jun@shizuha.com'],
            'architecture': ['aoi@shizuha.com'],
        })
        eng_task = Item.objects.create(
            title='engineering task — must stay in engineering pod',
            mode='task',
            status='open',
            priority='high',
            assignment_group='engineering',
            assignee_id=401,
            assignee_email='ryo@shizuha.com',
            workflow_name='autonomous-dev',
            project=project,
        )
        analytics_task = Item.objects.create(
            title='analytics task — must stay in analytics pod',
            mode='task',
            status='open',
            priority='high',
            assignment_group='analytics',
            assignee_id=402,
            assignee_email='jun@shizuha.com',
            workflow_name='autonomous-dev',
            project=project,
        )
        # Architecture member — must NEVER receive cross-team tasks.
        arch_member_email = 'aoi@shizuha.com'

        users = {
            'ryo@shizuha.com': (401, 'ryo@shizuha.com'),
            'jun@shizuha.com': (402, 'jun@shizuha.com'),
            'kai@shizuha.com': (403, 'kai@shizuha.com'),
            arch_member_email: (404, arch_member_email),
        }

        # Pod roster: engineering has ryo (1 task) + kai (0 tasks),
        # analytics has jun (1 task), architecture has aoi (0 tasks).
        # max_skew=0 forces all pods to rebalance. Architecture must never
        # absorb engineering or analytics tasks.
        with patch('tasks.user_service.user_service', self._fake_user_service(users)), \
             patch.object(WorkflowService, '_pod_roster', return_value={
                 'engineering': ['ryo@shizuha.com', 'kai@shizuha.com'],
                 'analytics': ['jun@shizuha.com'],
                 'architecture': [arch_member_email],
             }), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          return_value=True):
            result = sweep_rebalance_pods.run(max_skew=0)

        eng_task.refresh_from_db()
        analytics_task.refresh_from_db()

        self.assertNotEqual(eng_task.assignee_email, arch_member_email,
                            "engineering task must not cross to architecture pod")
        self.assertNotEqual(analytics_task.assignee_email, arch_member_email,
                            "analytics task must not cross to architecture pod")
        # Engineering task may move within engineering (to kai) — that's correct.
        self.assertIn(eng_task.assignee_email,
                      {'ryo@shizuha.com', 'kai@shizuha.com'},
                      "engineering task must stay within engineering pod")


class PLS157NativeMemberGuardTests(TestCase):
    """PLS-157: rebalancer must prefer native devops members over cross-team failover.

    Regression for PLAT-951 (bounced 3×): mika (devops tier-0) goes down → hiro
    (devops tier-1 AND architecture tier-0) had the lowest load, so the sweep kept
    picking hiro → task bounced back because hiro lacked host/kubectl access.
    Fix: _is_native_target() / _prefer_native() skips cross-pod tier-0 members when
    native peers are available; falls back to cross-pod only when no native exists.
    """

    # Common devops+architecture roster used by all tests.
    _ROSTER = {
        'devops': [['mika@shizuha.com'], ['ichi@shizuha.com', 'ni@shizuha.com', 'hiro@shizuha.com']],
        'architecture': [['hiro@shizuha.com']],
    }
    _USERS = {
        'mika@shizuha.com': (501, 'mika@shizuha.com'),
        'ichi@shizuha.com': (502, 'ichi@shizuha.com'),
        'ni@shizuha.com':   (503, 'ni@shizuha.com'),
        'hiro@shizuha.com': (504, 'hiro@shizuha.com'),
    }

    def setUp(self):
        """Materialize the legacy overlapping-roster scenario under test.

        The current model prevents a member from being added to two teams in the
        same organization. PLS-157 is nevertheless a compatibility guard for
        older rows that predate that invariant, so construct that invalid legacy
        state narrowly instead of depending on whichever migration seed happens
        to be present in the test database.
        """
        from tasks.models import Project, TeamRoster

        self.project = Project.objects.create(
            org_id=1,
            name='PLS-157 Failover',
            key='PLS157',
        )
        TeamRoster._single_membership_guard = True
        try:
            for team_name, tiers in self._ROSTER.items():
                TeamRoster.objects.update_or_create(
                    organization_id=1,
                    team_name=team_name,
                    defaults={
                        'organization_slug': 'shizuha',
                        'tiers': tiers,
                        'is_active': True,
                    },
                )
        finally:
            TeamRoster._single_membership_guard = False
        WorkflowService._invalidate_team_roster_caches()

    def _fake_user_service(self, users):
        class FakeUserService:
            def get_user_by_email(self, email):
                rec = users.get((email or '').strip().lower())
                return {'id': rec[0], 'email': rec[1]} if rec else None

            def get_user_by_id(self, user_id):
                for uid, email in users.values():
                    if int(user_id) == uid:
                        return {'id': uid, 'email': email}
                return None
        return FakeUserService()

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
    })
    def test_failover_drain_skips_cross_pod_tier0_when_native_available(self):
        """PLS-157 / PLAT-951: mika down, hiro is lightest but is arch tier-0.
        Drain must route to ichi (native, 1 task), NOT hiro (cross-pod, 0 tasks)."""
        from tasks.models import Item

        mika_task = Item.objects.create(
            title='devops task owned by mika (down)',
            mode='task', status='open', priority='high',
            assignment_group='devops',
            assignee_id=501, assignee_email='mika@shizuha.com',
            workflow_name='simple',
            project=self.project,
        )
        # Give ichi 1 active task so hiro (0 tasks) would win without PLS-157 guard.
        Item.objects.create(
            title='ichi active task (load=1)',
            mode='task', status='open', priority='normal',
            assignment_group='devops',
            assignee_id=502, assignee_email='ichi@shizuha.com',
            workflow_name='simple',
            project=self.project,
        )
        # ni has 2 active tasks — should not be preferred over ichi.
        for i in range(2):
            Item.objects.create(
                title=f'ni active task {i} (load=2)',
                mode='task', status='open', priority='normal',
                assignment_group='devops',
                assignee_id=503, assignee_email='ni@shizuha.com',
                workflow_name='simple',
                project=self.project,
            )

        # mika is DOWN; ichi/ni/hiro are available.
        available = {'ichi@shizuha.com', 'ni@shizuha.com', 'hiro@shizuha.com'}

        with patch('tasks.user_service.user_service', self._fake_user_service(self._USERS)), \
             patch.object(WorkflowService, '_pod_roster', return_value=self._ROSTER), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e in available):
            sweep_rebalance_pods.run()

        mika_task.refresh_from_db()
        self.assertEqual(
            mika_task.assignee_email, 'ichi@shizuha.com',
            "task must drain to native ichi (load=1), NOT hiro (cross-pod arch tier-0, load=0)",
        )

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
    })
    def test_failover_drain_fallback_to_cross_pod_when_no_native_available(self):
        """PLS-157: when only a cross-pod tier-0 member (hiro) is available, the task
        must still drain to hiro (no stranding)."""
        from tasks.models import Item

        mika_task = Item.objects.create(
            title='devops task owned by mika (down, only hiro available)',
            mode='task', status='open', priority='high',
            assignment_group='devops',
            assignee_id=501, assignee_email='mika@shizuha.com',
            workflow_name='simple',
            project=self.project,
        )

        # Only hiro is available; ichi and ni are also down.
        with patch('tasks.user_service.user_service', self._fake_user_service(self._USERS)), \
             patch.object(WorkflowService, '_pod_roster', return_value=self._ROSTER), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e == 'hiro@shizuha.com'):
            sweep_rebalance_pods.run()

        mika_task.refresh_from_db()
        self.assertEqual(
            mika_task.assignee_email, 'hiro@shizuha.com',
            "task must fall back to hiro when no native member is available (no stranding)",
        )

    @patch.dict(os.environ, {
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
    })
    def test_neutral_failover_member_treated_as_native(self):
        """PLS-157: a tier-1 member that is NOT tier-0 of any other pod (ichi, ni)
        is treated as native to every pod and always preferred over a cross-pod agent."""
        from tasks.models import Item

        mika_task = Item.objects.create(
            title='devops task to drain — ichi must be preferred over hiro',
            mode='task', status='open', priority='high',
            assignment_group='devops',
            assignee_id=501, assignee_email='mika@shizuha.com',
            workflow_name='simple',
            project=self.project,
        )
        # hiro has lower load (0) than ichi (1) without the native guard.
        Item.objects.create(
            title='ichi active task',
            mode='task', status='open', priority='normal',
            assignment_group='devops',
            assignee_id=502, assignee_email='ichi@shizuha.com',
            workflow_name='simple',
            project=self.project,
        )

        available = {'ichi@shizuha.com', 'hiro@shizuha.com'}

        with patch('tasks.user_service.user_service', self._fake_user_service(self._USERS)), \
             patch.object(WorkflowService, '_pod_roster', return_value=self._ROSTER), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: e in available):
            sweep_rebalance_pods.run()

        mika_task.refresh_from_db()
        # ichi is neutral (not tier-0 of any pod) → native; hiro is cross-pod → not native.
        # Even though hiro has lower load, ichi must be picked.
        self.assertEqual(
            mika_task.assignee_email, 'ichi@shizuha.com',
            "neutral failover member ichi must be treated as native and preferred over hiro",
        )


class CriticalOverrideBlockerRootTests(TestCase):
    """Operator 2026-08-08: stranded ULTIMATE blocker-roots must get the
    critical force-assign.

    The regular queue-drain is cap-enforced and fails-closed when the live fleet
    is at WIP capacity, so an urgent/high item in a normal status (open/todo/
    in_progress/in_review) that is itself an ultimate blocker-root (it has active
    blocked descendants, i.e. other tasks wait on it) had NO assignment path
    while seats were at cap — it sat unassigned for weeks while every descendant
    waited. The critical-override must now also pick up normally-handled-status
    blocker-roots (they are PLS-509 non-sheddable, so no assign/reclaim churn).
    """

    @patch.dict(os.environ, {
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
        'PULSE_WIP_CAP': '1',
        'PULSE_INFLIGHT_CAP': '1',
    })
    def test_stranded_normal_status_blocker_root_gets_critical_override(self):
        from tasks.models import Item, Project, TeamRoster

        project = Project.objects.create(
            org_id=1,
            name='Critical Override Blocker Root',
            key='COBR',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['kai@shizuha.com']],
                'is_active': True,
            },
        )

        # The ultimate blocker-root: urgent, open status, no assignee, but with an
        # active blocked descendant that waits on it.
        root = Item.objects.create(
            title='ultimate blocker root',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
            project=project,
        )
        # A descendant in a live status that is linked blocked-by root. Create the
        # ItemLink directly (as satisfy_blocked_invariant does) so root stays
        # UNASSIGNED — the synthetic-owner helper would claim root, defeating the
        # stranded-item premise.
        from tasks.models import ItemLink
        descendant = Item.objects.create(
            title='depends on root',
            mode='task',
            status='open',
            priority='high',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
            project=project,
        )
        link = ItemLink(
            source_item=root,
            target_item=descendant,
            link_type='blocks',
            created_by_id=0,
            created_by_email='test@pulse',
        )
        link._skip_auto_block = True
        link.save()

        class FakeUserService:
            def get_user_by_email(self, email):
                return {'id': 502, 'email': email} if email == 'kai@shizuha.com' else None

            def get_user_by_id(self, user_id):
                return (
                    {'id': 502, 'email': 'kai@shizuha.com'}
                    if int(user_id) == 502 else None
                )

        with patch('tasks.user_service.user_service', FakeUserService()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          return_value=True):
            from tasks.tasks import _assign_critical_unassigned
            assigned_count = _assign_critical_unassigned()

        root.refresh_from_db()
        self.assertGreaterEqual(assigned_count, 1,
                                'stranded critical blocker-root must be force-assigned')
        self.assertEqual(root.assignee_email, 'kai@shizuha.com')
        self.assertIsNotNone(root.assignee_id)

    @patch.dict(os.environ, {
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
        'PULSE_WIP_CAP': '1',
        'PULSE_INFLIGHT_CAP': '1',
    })
    def test_critical_override_dwell_no_refire_after_reclaim(self):
        """PLAT-9067: the override must not re-fire within the dwell window
        absent a task-state change.

        Live defect: the assign → (PLS-509 WIP-overload reclaim) → re-assign
        cycle posted 4× identical force-assign notices inside one hour for
        SCLI-348. After a force-assign, a reclaim that clears the assignee
        makes the item look stranded again on the next 10m sweep tick; the
        override then re-fires and posts another identical notice. The dwell
        gate (default 30m, kei's floor) must skip the re-fire while the
        override notice is recent, and must allow assignment again once the
        notice ages past the window.
        """
        from datetime import timedelta
        from django.utils import timezone
        from tasks.models import Item, ItemLink, Comment, Project, TeamRoster

        project = Project.objects.create(
            org_id=1,
            name='Critical Override Dwell',
            key='CODW',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['kai@shizuha.com']],
                'is_active': True,
            },
        )

        root = Item.objects.create(
            title='dwell regression blocker root',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
            project=project,
        )
        descendant = Item.objects.create(
            title='depends on dwell root',
            mode='task',
            status='open',
            priority='high',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
            project=project,
        )
        link = ItemLink(
            source_item=root,
            target_item=descendant,
            link_type='blocks',
            created_by_id=0,
            created_by_email='test@pulse',
        )
        link._skip_auto_block = True
        link.save()

        class FakeUserService:
            def get_user_by_email(self, email):
                return {'id': 502, 'email': email} if email == 'kai@shizuha.com' else None

            def get_user_by_id(self, user_id):
                return (
                    {'id': 502, 'email': 'kai@shizuha.com'}
                    if int(user_id) == 502 else None
                )

        def _run_override():
            with patch('tasks.user_service.user_service', FakeUserService()), \
                 patch.object(WorkflowService, '_is_agent_available_for_assignment',
                              return_value=True):
                from tasks.tasks import _assign_critical_unassigned
                return _assign_critical_unassigned()

        # Tick 1: stranded root gets force-assigned + one override notice.
        self.assertGreaterEqual(_run_override(), 1)
        root.refresh_from_db()
        self.assertEqual(root.assignee_email, 'kai@shizuha.com')
        override_comments = Comment.objects.filter(
            item=root, author_email='system@pulse',
            content__startswith='🚨 **Critical unassigned override**',
        )
        self.assertEqual(override_comments.count(), 1)

        # Simulate the PLS-509 WIP-overload reclaim: assignee cleared, item
        # stranded again, notice still fresh. The production clear command
        # (_apply_assignment_clear_command) writes NULL for BOTH fields —
        # an empty-string assignee_email is a state production never writes
        # and the override's candidate query (assignee_email__isnull=True)
        # would not see it, silently dropping the item from the sweep.
        root.assignee_id = None
        root.assignee_email = None
        root.save(update_fields=['assignee_id', 'assignee_email', 'updated_at'])

        # Tick 2 within the dwell window: must NOT re-fire.
        self.assertEqual(
            _run_override(), 0,
            'override must dwell after a recent force-assign notice',
        )
        root.refresh_from_db()
        self.assertIsNone(
            root.assignee_email,
            'dwell must not re-assign within the window',
        )
        self.assertEqual(
            override_comments.count(), 1,
            'no second identical override notice within the dwell window',
        )

        # Age the notice past the dwell window: assignment must work again.
        notice = override_comments.first()
        Comment.objects.filter(pk=notice.pk).update(
            created_at=timezone.now() - timedelta(minutes=45),
        )
        self.assertGreaterEqual(_run_override(), 1)
        root.refresh_from_db()
        self.assertEqual(root.assignee_email, 'kai@shizuha.com')

    @patch.dict(os.environ, {
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
        'PULSE_WIP_CAP': '1',
        'PULSE_INFLIGHT_CAP': '1',
    })
    def test_blocked_backlog_does_not_starve_newer_blocker_roots(self):
        """PLAT-5261: ancient `blocked` items with an active upstream blocker
        must not consume the critical-override's 50-candidate window.

        The override takes the 50 OLDEST urgent/high unassigned candidates
        (``order_by('-priority','created_at')[:50]``). The historical critical
        backlog is dominated by stale ``blocked`` items that STILL carry a live
        inbound block link — the assignment loop's active-blocker guard skips
        every one of them, so if they fill the window no actionable blocker-root
        is ever assigned (verified live: 0/50 assignable while 82 stranded roots
        waited outside). The SQL-level upstream-blocker filter must exclude them
        BEFORE the [:50] cap so newer actionable roots enter the window.
        """
        from datetime import timedelta
        from django.utils import timezone
        from tasks.models import Item, ItemLink, Project, TeamRoster

        project = Project.objects.create(
            org_id=1, name='Window Starvation', key='COBRK',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['kai@shizuha.com']],
                'is_active': True,
            },
        )

        # 51 ANCIENT unassigned urgent items in `blocked` status — created
        # through the valid PLAT-4215 path (create_blocked_item), with an active
        # upstream blocker, then backdated so they fill the entire 50-slot
        # candidate window when sorted oldest-first. Every one of them is
        # skipped by the override's active-blocker guard, so without the
        # SQL-level filter they starve the newer actionable blocker-root.
        from .base import create_blocked_item
        old = timezone.now() - timedelta(days=30)
        created_pks = []
        for i in range(51):
            blocked = create_blocked_item(
                title=f'ancient blocked backlog {i}',
                mode='task',
                priority='urgent',
                assignment_group='engineering',
                project=project,
            )
            created_pks.append(blocked.pk)
        # Backdate (created_at is auto_now_add, so a direct UPDATE is the only
        # way to age the fixtures for the oldest-first ordering).
        Item.objects.filter(pk__in=created_pks).update(
            created_at=old - timedelta(hours=1),
        )

        # The NEWER actionable blocker-root (created now, so after all 51 old
        # items in created_at ordering) with an active blocked descendant.
        root = Item.objects.create(
            title='newer stranded blocker root',
            mode='task',
            status='todo',
            priority='urgent',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
            project=project,
        )
        descendant = Item.objects.create(
            title='depends on new root',
            mode='task',
            status='open',
            priority='high',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
            project=project,
        )
        link = ItemLink(
            source_item=root,
            target_item=descendant,
            link_type='blocks',
            created_by_id=0,
            created_by_email='test@pulse',
        )
        link._skip_auto_block = True
        link.save()

        class FakeUserService:
            def get_user_by_email(self, email):
                return {'id': 502, 'email': email} if email == 'kai@shizuha.com' else None

            def get_user_by_id(self, user_id):
                return (
                    {'id': 502, 'email': 'kai@shizuha.com'}
                    if int(user_id) == 502 else None
                )

        with patch('tasks.user_service.user_service', FakeUserService()), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          return_value=True):
            from tasks.tasks import _assign_critical_unassigned
            assigned_count = _assign_critical_unassigned()

        root.refresh_from_db()
        # The 51 old blocked items are skipped by the guard AND now filtered at
        # SQL level; the newer root must be assigned.
        self.assertGreaterEqual(assigned_count, 1,
                                'newer blocker-root must not be starved by the blocked backlog')
        self.assertEqual(root.assignee_email, 'kai@shizuha.com',
                         'the starved blocker-root must get its concrete seat')

    @patch.dict(os.environ, {
        'PULSE_HEALTH_EXPORTER_URL': 'disabled',
        'PULSE_API_HEALTH_URL': 'disabled',
        'PULSE_WIP_CAP_TEAMS': 'engineering,qa',
        'PULSE_WIP_CAP': '1',
        'PULSE_INFLIGHT_CAP': '1',
    })
    def test_critical_override_respects_recent_routing_hold(self):
        """PLAT-9067 (kai 09-18 scope extension): the override must not
        force-assign against a fresh resolver routability hold.

        Live defect (HIVE-748): an urgent in_review item capability-walled on
        ALL qa seats (visual critique; text-only model) and explicitly routed
        to a vision-capable reviewer was force-assigned to text-only qa seats
        ~30× overnight by the critical-unassigned override, alternating with
        PLS-509 stale-owner removals — an override↔removal fight that can
        never converge. The resolver had already recorded the no-candidate
        verdict (PLS-278 hold, origin_event='resolver.routability_hold'); the
        override must read that hold and skip while it is fresh. Once the
        hold ages past the dwell window, the override may retry (seats may
        have come online or the hold cleared).
        """
        from datetime import timedelta
        from django.utils import timezone
        from tasks.models import Item, ItemLink, Comment, Project, TeamRoster

        project = Project.objects.create(
            org_id=1,
            name='Critical Override Hold Gate',
            key='COHG',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['kai@shizuha.com']],
                'is_active': True,
            },
        )

        root = Item.objects.create(
            title='hold-gated blocker root',
            mode='task',
            status='open',
            priority='urgent',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
            project=project,
        )
        descendant = Item.objects.create(
            title='depends on hold-gated root',
            mode='task',
            status='open',
            priority='high',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
            project=project,
        )
        link = ItemLink(
            source_item=root,
            target_item=descendant,
            link_type='blocks',
            created_by_id=0,
            created_by_email='test@pulse',
        )
        link._skip_auto_block = True
        link.save()

        # The resolver's no-candidate verdict, fresh (PLS-278 shape).
        Comment.objects.create(
            item=root,
            author_id=0,
            author_email='system@pulse',
            origin_kind='system',
            origin_event='resolver.routability_hold',
            origin_payload={'dedupe': 'PLS-278 routability hold [sweep:engineering:seat_incapable]'},
            content=(
                'PLS-278 routability hold [sweep:engineering:seat_incapable]\n'
                'No routable candidate for team `engineering`.'
            ),
        )

        class FakeUserService:
            def get_user_by_email(self, email):
                return {'id': 502, 'email': email} if email == 'kai@shizuha.com' else None

            def get_user_by_id(self, user_id):
                return (
                    {'id': 502, 'email': 'kai@shizuha.com'}
                    if int(user_id) == 502 else None
                )

        def _run_override():
            with patch('tasks.user_service.user_service', FakeUserService()), \
                 patch.object(WorkflowService, '_is_agent_available_for_assignment',
                              return_value=True):
                from tasks.tasks import _assign_critical_unassigned
                return _assign_critical_unassigned()

        # Fresh hold: the override must NOT force-assign into the walled seat.
        self.assertEqual(
            _run_override(), 0,
            'override must skip an item under a fresh resolver routability hold',
        )
        root.refresh_from_db()
        self.assertIsNone(
            root.assignee_email,
            'fresh routing hold must prevent force-assignment',
        )
        self.assertEqual(
            Comment.objects.filter(
                item=root, author_email='system@pulse',
                content__startswith='🚨 **Critical unassigned override**',
            ).count(),
            0,
            'no override notice may be posted against a fresh routing hold',
        )

        # Age the hold past the dwell window: the override may retry — the
        # seat landscape may have changed since the verdict.
        Comment.objects.filter(item=root, origin_event='resolver.routability_hold').update(
            created_at=timezone.now() - timedelta(minutes=45),
        )
        self.assertGreaterEqual(_run_override(), 1)
        root.refresh_from_db()
        self.assertEqual(
            root.assignee_email, 'kai@shizuha.com',
            'override may assign once the routing hold ages past the window',
        )


class CriticalOverrideAdmissionDispatchTests(TestCase):
    """HIVE-1828: the critical-unassigned override must route through the
    admission-capacity boundary so it cannot flood one admission-saturated seat.

    Regression method: exercise `_assign_critical_unassigned` with a patched
    `_resolve_team_assignee` that simulates the seat outcomes, asserting the
    DISPATCH contract of the fix:
      - a non-blocker-root candidate whose team has no admission headroom is
        left group-queued (NOT force-assigned), stopping the flood;
      - a stranded blocker-root whose team is saturated is still force-assigned
        via the unconditional override fallback (preserves PLS-509 non-sheddable
        WIP / the pre-existing CriticalOverrideBlockerRootTests contract).
    """

    def _project_and_roster(self):
        from tasks.models import Project, TeamRoster
        project = Project.objects.create(
            org_id=1, name='OVR Dispatch', key='OVRDI',
        )
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [['kai@shizuha.com']],
                'is_active': True,
                'wip_enforced': True,
                'active_wip_cap': 1,
                'ready_wip_cap': 1,
            },
        )
        return project

    def _candidate(self, project, *, status='scheduled'):
        from tasks.models import Item
        return Item.objects.create(
            title='override candidate',
            mode='task',
            status=status,
            priority='urgent',
            assignment_group='engineering',
            workflow_name='autonomous-dev',
            project=project,
        )

    def _link_block(self, root, target):
        from tasks.models import ItemLink
        link = ItemLink(
            source_item=root,
            target_item=target,
            link_type='blocks',
            created_by_id=0,
            created_by_email='test@pulse',
        )
        link._skip_auto_block = True
        link.save()

    @patch.object(WorkflowService, '_resolve_team_assignee',
                  return_value=(None, None))
    def test_non_blocker_root_left_group_queued_when_no_headroom(self, _resolve):
        """No seat has admission headroom and the item is NOT a blocker-root:
        the override must NOT force-assign it (this is the flood stop)."""
        project = self._project_and_roster()
        cand = self._candidate(project)  # no blocks link -> not a blocker-root
        from tasks.tasks import _assign_critical_unassigned
        assigned = _assign_critical_unassigned()
        cand.refresh_from_db()
        self.assertEqual(assigned, 0, 'no headroom + non-blocker-root: no assign')
        self.assertIsNone(cand.assignee_email)
        self.assertIsNone(cand.assignee_id)
        # Must have attempted only the capacity-aware (enforce_cap=True)
        # resolution — never the unconditional override.
        calls = [c.kwargs.get('enforce_cap') for c in _resolve.call_args_list]
        self.assertNotEqual(calls, [], 'resolver must be consulted')
        self.assertTrue(all(flag is True for flag in calls),
                        f'capacity-aware resolution expected, got {calls}')

    @patch.object(WorkflowService, '_resolve_team_assignee',
                  side_effect=[(None, None), (500, 'kai@shizuha.com')])
    def test_blocker_root_still_force_assigned_via_override_fallback(
        self, _resolve,
    ):
        """Stranded blocker-root with a saturated team must still get a
        concrete owner through the unconditional override fallback."""
        project = self._project_and_roster()
        root = self._candidate(project, status='open')
        target = self._candidate(project, status='open')
        self._link_block(root, target)  # real blocks link -> reads as blocker-root
        from tasks.tasks import _assign_critical_unassigned
        assigned = _assign_critical_unassigned()
        root.refresh_from_db()
        self.assertGreaterEqual(assigned, 1,
                                'stranded blocker-root must be force-assigned')
        self.assertEqual(root.assignee_email, 'kai@shizuha.com')
        # First call capacity-aware (True); fallback call unconditional (False).
        flags = [c.kwargs.get('enforce_cap') for c in _resolve.call_args_list]
        self.assertEqual(flags, [True, False],
                         f'expected [True, False] dispatch, got {flags}')


class AwaitingDeployCarrierExclusionTests(SimpleTestCase):
    """PLAT-9151: awaiting_deploy carriers are held provenance, not WIP seats.

    Migration 0148 seeds the global awaiting_deploy Status with
    category='in_progress' (a DISPLAY category). Before this fix the active
    slot count read that category and counted every deploy carrier as a
    hands-on seat — merge seats legitimately holding ~20 carriers saturated
    the cap and denied Start Progress (WIP_ACTIVE_CAPACITY_DENIED). The
    classifier must return None for awaiting_deploy, and neither slot
    dimension may count it.
    """

    def test_classifier_returns_none_for_awaiting_deploy(self):
        # SimpleTestCase: the hardcoded exclusion core must win WITHOUT a DB
        # read (PLS-737 degradation path) — the slug is in the hardcoded set.
        self.assertIsNone(
            WorkflowService._slot_kind_for_status('awaiting_deploy'),
            'awaiting_deploy carriers must classify not_applicable (no '
            'reservation on entry, no count in either dimension)',
        )

    def test_awaiting_deploy_in_assignment_excluded_core(self):
        excluded = WorkflowService._assignment_excluded_statuses()
        self.assertIn('awaiting_deploy', excluded)

    def test_blocked_still_excluded_alongside_awaiting_deploy(self):
        # The ruling names both: blocked was already excluded; awaiting_deploy
        # joins it. Neither may regress.
        excluded = WorkflowService._assignment_excluded_statuses()
        self.assertIn('blocked', excluded)
        self.assertIn('awaiting_deploy', excluded)


class PLS843AtomicWipAdmissionTests(TransactionTestCase):
    EMAIL = 'wip-seat@example.com'
    UID = 9509

    def setUp(self):
        from tasks.models import Project
        self.project = Project.objects.create(
            org_id=1,
            name='PLS-843 Atomic WIP Admission',
            key='P843AWIP',
        )

    def test_engineering_active_work_still_consumes_active_admission(self):
        """The selective exemption must not turn into a removed cap."""
        from tasks.models import Item, TeamRoster
    
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.EMAIL]],
                'junior_emails': [self.EMAIL],
                'is_active': True,
                'wip_enforced': True,
                'ready_wip_cap': 1,
                'active_wip_cap': 1,
            },
        )
        owner = Item.objects.create(
            title='hands-on engineering owner',
            mode='task',
            status='in_progress',
            priority='high',
            assignment_group='engineering',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )
        candidate = Item.objects.create(
            title='second hands-on engineering item',
            mode='task',
            status='open',
            # 'high', NOT 'urgent': urgent work bypasses the WIP cap by
            # operator ruling 2026-09-27 (_priority_ignores_wip_cap), so an
            # urgent candidate would be admitted even at cap and this test
            # could not exercise the denial path (run 8377).
            priority='high',
            assignment_group='engineering',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )

        admitted = WorkflowService._reserve_wip_slot(
            candidate,
            self.UID,
            self.EMAIL,
            status_override='in_progress',
        )

        self.assertFalse(admitted)
        self.assertEqual(candidate._wip_capacity_diagnostics['actual'], 1)
        self.assertEqual(
            candidate._wip_capacity_diagnostics['reservation_owner'],
            owner.item_key or str(owner.pk),
        )
    
    def test_engineering_review_wait_does_not_consume_active_admission(self):
        """Review latency stays owned but cannot remove authoring capacity."""
        from tasks.models import Item, TeamRoster
    
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.EMAIL]],
                'junior_emails': [self.EMAIL],
                'is_active': True,
                'wip_enforced': True,
                'ready_wip_cap': 1,
                'active_wip_cap': 1,
            },
        )
        waiting_review = Item.objects.create(
            title='owned work waiting on Review',
            mode='task',
            status='in_review',
            priority='high',
            assignment_group='engineering',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )
        waiting_blocked = create_blocked_item(
            title='owned work waiting on a blocker',
            mode='task',
            priority='urgent',
            assignment_group='engineering',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )
        candidate = Item.objects.create(
            title='one workable authoring item',
            mode='task',
            status='open',
            # 'high', not 'urgent': proves the waiting-only admission comes
            # from the PLS-843 role-excluded carve-out itself, not from the
            # operator-ruled urgent bypass (_priority_ignores_wip_cap).
            priority='high',
            assignment_group='engineering',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )

        admitted = WorkflowService._reserve_wip_slot(
            candidate,
            self.UID,
            self.EMAIL,
            status_override='in_progress',
        )

        self.assertTrue(admitted)
        self.assertEqual(candidate._wip_capacity_diagnostics['actual'], 0)
        self.assertEqual(waiting_review.assignee_email, self.EMAIL)
        self.assertEqual(waiting_blocked.assignee_email, self.EMAIL)
        WorkflowService._release_unconsumed_wip_reservation(candidate)
    
    # INFLIGHT_CAP must be 2, not 1: the mid-test polarity asserts
    # at_capacity==False while workable load is already 1 (the waiting
    # carrier), and the final polarity asserts at_capacity==True at
    # workable==2 (hands-on added). With cap=1 the mid-test assertion is
    # unsatisfiable (1>=1 → True) — run 8418's deterministic red.
    @patch.dict(os.environ, {'PULSE_WIP_CAP': '0', 'PULSE_INFLIGHT_CAP': '2'})
    def test_real_waiting_load_is_assignable_then_real_active_load_is_full(self):
        """Production load queries preserve both admission polarities."""
        from tasks.models import Item, TeamRoster
    
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='engineering',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[self.EMAIL]],
                'junior_emails': [self.EMAIL],
                'is_active': True,
                'wip_enforced': True,
                'ready_wip_cap': 1,
                'active_wip_cap': 1,
            },
        )
        WorkflowService._invalidate_team_roster_caches()
    
        Item.objects.create(
            title='review-owned but author-waiting carrier',
            mode='task',
            status='in_review',
            priority='high',
            assignment_group='engineering',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )
        create_blocked_item(
            title='blocked ownership remains visible',
            mode='task',
            priority='urgent',
            assignment_group='engineering',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )
    
        self.assertEqual(
            WorkflowService._agent_active_load(self.UID, self.EMAIL),
            0,
        )
        self.assertEqual(
            WorkflowService._agent_workable_load(self.UID, self.EMAIL),
            1,
        )
        self.assertFalse(
            WorkflowService._agent_at_capacity(self.UID, self.EMAIL),
        )
    
        Item.objects.create(
            title='the one admitted hands-on task',
            mode='task',
            status='in_progress',
            priority='urgent',
            assignment_group='engineering',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            workflow_name='autonomous-dev',
            project=self.project,
        )
    
        self.assertEqual(
            WorkflowService._agent_active_load(self.UID, self.EMAIL),
            1,
        )
        self.assertTrue(
            WorkflowService._agent_at_capacity(self.UID, self.EMAIL),
        )
    
    # INFLIGHT_CAP must be 11, not 10: the headroom polarity asserts
    # at_capacity==False at inflight load 10, and _at_capacity treats
    # load >= cap as full — with cap=10 that assertion is unsatisfiable
    # (10>=10 → True), the same arithmetic trap run 8418 caught at cap=1.
    # At cap=11: (0,10) is admissible and (1,11) is the exact runaway bound.
    @patch.dict(os.environ, {'PULSE_WIP_CAP': '5', 'PULSE_INFLIGHT_CAP': '11'})
    def test_waiting_only_queue_receives_one_hands_on_slot(self):
        # Ten review/QA/merge-owned carriers remain visible and owned, but an
        # otherwise idle author must still be admitted to one workable item.
        self.assertFalse(WorkflowService._at_capacity(0, 10))
        # The headroom is exactly one hands-on slot, not removal of the runaway
        # bound: after work is admitted, the same total load is full again.
        self.assertTrue(WorkflowService._at_capacity(1, 11))
