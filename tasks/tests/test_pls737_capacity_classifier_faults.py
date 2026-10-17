"""PLS-737: Status-table degradation must not manufacture seats or silent admits.

Contract (@hiro):
  healthy domain: ``active | ready | None`` (None = legitimate not_applicable)
  degraded:       raise ``CapacityClassificationUnavailable``
                  — never spelled as ``None`` or ``'ready'``

Triggers:
  1. Status query raises
  2. Status row absent (``.first()`` returns None with no exception)

Caller policy:
  * focus/comment guards → non-fire
  * admission / reservation / routability → hold, typed ``capacity_unavailable``
  * verify → never release reservation as not_applicable

Acceptance #4: every fault assertion proves the injection bites first.
Acceptance #5: fixtures from DB-category-derived set, not ``_WORKING_STATUSES``.
"""

from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from tasks.models import Status
from tasks.services import workflow_service as ws
from tasks.services.workflow_service import (
    CAPACITY_UNAVAILABLE,
    CapacityClassificationUnavailable,
    WorkflowService,
)


class _Boom(Exception):
    """The injected Status-table fault."""


def _raise(*_a, **_kw):
    raise _Boom('injected Status-table fault')


class CapacityClassifierFaultTests(TestCase):
    def setUp(self):
        Status.objects.update_or_create(
            slug='backlog', defaults={'name': 'Backlog', 'category': 'backlog'},
        )
        Status.objects.update_or_create(
            slug='open', defaults={'name': 'Open', 'category': 'todo'},
        )
        Status.objects.update_or_create(
            slug='scheduled', defaults={'name': 'Scheduled', 'category': 'scheduled'},
        )
        # DB-category-derived ready status — NOT in the open/todo/pending
        # short-circuit set, so fault injection reaches the fallible read.
        Status.objects.update_or_create(
            slug='needs_triage', defaults={'name': 'Needs Triage', 'category': 'todo'},
        )
        ws._CAPACITY_CLASSIFIER_DEGRADED.clear()

    # ---- acceptance #4: prove the injection bites before trusting a result ---

    def test_injection_is_effective_before_any_fault_measurement(self):
        with mock.patch.object(Status.objects, 'filter', side_effect=_raise):
            with self.assertRaises(_Boom):
                Status.objects.filter(slug='backlog').values_list(
                    'category', flat=True,
                ).first()

    # ---- healthy controls ----------------------------------------------------

    def test_backlog_classifies_none_when_healthy(self):
        self.assertIsNone(WorkflowService._slot_kind_for_status('backlog'))

    def test_scheduled_classifies_none_when_healthy(self):
        self.assertIsNone(WorkflowService._slot_kind_for_status('scheduled'))

    def test_blocked_hardcoded_exclusion_is_none(self):
        self.assertIsNone(WorkflowService._slot_kind_for_status('blocked'))

    def test_open_classifies_ready_when_healthy(self):
        self.assertEqual(WorkflowService._slot_kind_for_status('open'), 'ready')

    def test_db_derived_ready_classifies_ready_when_healthy(self):
        self.assertEqual(WorkflowService._slot_kind_for_status('needs_triage'), 'ready')

    def test_consumes_capacity_healthy_matrix(self):
        self.assertFalse(WorkflowService._consumes_capacity('backlog'))
        self.assertFalse(WorkflowService._consumes_capacity('blocked'))
        self.assertTrue(WorkflowService._consumes_capacity('open'))
        self.assertTrue(WorkflowService._consumes_capacity('needs_triage'))
        self.assertTrue(WorkflowService._consumes_capacity('in_progress'))

    # ---- trigger 1: injected exception ---------------------------------------

    def test_backlog_raises_unavailable_under_fault_not_ready(self):
        with mock.patch.object(Status.objects, 'filter', side_effect=_raise):
            with self.assertRaises(_Boom):
                Status.objects.filter(slug='x').first()
            with self.assertRaises(CapacityClassificationUnavailable) as ctx:
                WorkflowService._slot_kind_for_status('backlog')
        self.assertEqual(ctx.exception.reason, 'read_failed')
        self.assertNotEqual(ctx.exception.reason, 'ready')

    def test_consumes_capacity_propagates_unavailable(self):
        with mock.patch.object(Status.objects, 'filter', side_effect=_raise):
            with self.assertRaises(CapacityClassificationUnavailable):
                WorkflowService._consumes_capacity('backlog')

    # ---- trigger 2: absent Status row ----------------------------------------

    def test_absent_row_raises_unavailable_not_ready(self):
        Status.objects.filter(slug='backlog').delete()
        with self.assertRaises(CapacityClassificationUnavailable) as ctx:
            WorkflowService._slot_kind_for_status('backlog')
        self.assertEqual(ctx.exception.reason, 'row_absent')

    def test_absent_and_raised_are_same_typed_unavailable(self):
        """Both triggers produce CapacityClassificationUnavailable, not 'ready'."""
        Status.objects.filter(slug='backlog').delete()
        with self.assertRaises(CapacityClassificationUnavailable) as absent_ctx:
            WorkflowService._slot_kind_for_status('backlog')

        Status.objects.update_or_create(
            slug='backlog', defaults={'name': 'Backlog', 'category': 'backlog'},
        )
        with mock.patch.object(Status.objects, 'filter', side_effect=_raise):
            with self.assertRaises(CapacityClassificationUnavailable) as raised_ctx:
                WorkflowService._slot_kind_for_status('backlog')

        self.assertIsInstance(absent_ctx.exception, CapacityClassificationUnavailable)
        self.assertIsInstance(raised_ctx.exception, CapacityClassificationUnavailable)
        self.assertEqual(absent_ctx.exception.reason, 'row_absent')
        self.assertEqual(raised_ctx.exception.reason, 'read_failed')

    # ---- acceptance #3: observable -------------------------------------------

    def test_fault_emits_a_log_and_increments_a_counter(self):
        with mock.patch.object(Status.objects, 'filter', side_effect=_raise):
            with self.assertRaises(_Boom):
                Status.objects.filter(slug='x').first()
            with self.assertLogs(ws.logger, level='WARNING') as caught:
                with self.assertRaises(CapacityClassificationUnavailable):
                    WorkflowService._slot_kind_for_status('backlog')
        self.assertTrue(
            any('PLS-737' in line for line in caught.output),
            msg=f'degradation must be logged, got {caught.output!r}',
        )
        self.assertGreater(sum(ws._CAPACITY_CLASSIFIER_DEGRADED.values()), 0)

    # ---- acceptance #5: partition --------------------------------------------

    def test_working_status_literals_never_reach_the_fallible_read(self):
        with mock.patch.object(Status.objects, 'filter', side_effect=_raise):
            with self.assertRaises(_Boom):
                Status.objects.filter(slug='x').first()
            for slug in sorted(WorkflowService._WORKING_STATUSES):
                with self.subTest(slug=slug):
                    self.assertEqual(WorkflowService._slot_kind_for_status(slug), 'active')

    def test_hardcoded_exclusions_are_also_immune(self):
        with mock.patch.object(Status.objects, 'filter', side_effect=_raise):
            with self.assertRaises(_Boom):
                Status.objects.filter(slug='x').first()
            for slug in ('blocked', 'deferred', 'cancelled'):
                with self.subTest(slug=slug):
                    self.assertIsNone(WorkflowService._slot_kind_for_status(slug))

    # ---- caller boundary: guards non-fire ------------------------------------

    def test_guard_helper_non_fires_under_fault(self):
        with mock.patch.object(Status.objects, 'filter', side_effect=_raise):
            with self.assertRaises(_Boom):
                Status.objects.filter(slug='x').first()
            # Non-fire: returns None so ``== 'active'`` / ``!= 'active'`` skip.
            self.assertIsNone(WorkflowService._slot_kind_for_guard('backlog'))
            self.assertIsNone(WorkflowService._slot_kind_for_guard('needs_triage'))

    # ---- caller boundary: reservation does not silent-admit ------------------

    def test_reserve_returns_capacity_unavailable_not_none_under_fault(self):
        """None means legitimate no-seat; unavailable must be a distinct sentinel."""
        item = SimpleNamespace(
            pk=1,
            item_key='PLS-737-T',
            mode='task',
            status='needs_triage',
            assignment_group='engineering',
            assignee_id=None,
            assignee_email='nagi@shizuha.com',
            project=None,
        )
        roster = SimpleNamespace(
            organization_slug='shizuha',
            organization_id=1,
            team_name='engineering',
            active_wip_cap=2,
            ready_wip_cap=4,
            wip_enforced=True,
        )
        with mock.patch.object(
            WorkflowService, '_wip_roster_config', return_value=roster,
        ):
            with mock.patch.object(Status.objects, 'filter', side_effect=_raise):
                with self.assertRaises(_Boom):
                    Status.objects.filter(slug='x').first()
                admitted = WorkflowService._reserve_wip_slot(
                    item, None, 'nagi@shizuha.com',
                    status_override='needs_triage',
                )
        self.assertEqual(admitted, CAPACITY_UNAVAILABLE)
        self.assertIsNot(admitted, None)
        self.assertIsNot(admitted, False)
        self.assertEqual(
            getattr(item, '_wip_capacity_diagnostics', {}).get('error'),
            CAPACITY_UNAVAILABLE,
        )

    def test_reserve_two_production_order_attempts_stay_unavailable(self):
        """Stateful recurrence: two top-level attempts both refuse (no re-arm admit)."""
        item = SimpleNamespace(
            pk=2,
            item_key='PLS-737-T2',
            mode='task',
            status='needs_triage',
            assignment_group='engineering',
            assignee_id=None,
            assignee_email='nagi@shizuha.com',
            project=None,
        )
        roster = SimpleNamespace(
            organization_slug='shizuha',
            organization_id=1,
            team_name='engineering',
            active_wip_cap=2,
            ready_wip_cap=4,
            wip_enforced=True,
        )
        results = []
        with mock.patch.object(
            WorkflowService, '_wip_roster_config', return_value=roster,
        ):
            with mock.patch.object(Status.objects, 'filter', side_effect=_raise):
                with self.assertRaises(_Boom):
                    Status.objects.filter(slug='x').first()
                for _ in range(2):
                    results.append(
                        WorkflowService._reserve_wip_slot(
                            item, None, 'nagi@shizuha.com',
                            status_override='needs_triage',
                        )
                    )
        self.assertEqual(results, [CAPACITY_UNAVAILABLE, CAPACITY_UNAVAILABLE])

    # ---- caller boundary: verify never releases on unavailable ---------------

    def test_verify_does_not_release_reservation_on_unavailable(self):
        item = SimpleNamespace(
            pk=3,
            item_key='PLS-737-V',
            mode='task',
            status='needs_triage',
            assignment_group='engineering',
            assignee_id=7,
            assignee_email='nagi@shizuha.com',
            _wip_reservation_token='tok-pls737',
        )
        released = {'called': False}

        def _fake_release(it):
            released['called'] = True

        # Minimal reservation/capacity stubs so we reach the classification read.
        fake_res = SimpleNamespace(
            capacity_id=1,
            expires_at=mock.Mock(),
            created_at=mock.Mock(),
            assignment_group='engineering',
            slot_kind='ready',
            capacity=SimpleNamespace(pk=1, assignee_id=7, assignee_email='nagi@shizuha.com'),
        )
        # expires_at > now, created_at recent
        from django.utils import timezone
        from datetime import timedelta
        now = timezone.now()
        fake_res.expires_at = now + timedelta(minutes=5)
        fake_res.created_at = now - timedelta(seconds=5)

        with mock.patch(
            'tasks.models.WipSlotReservation.objects'
        ) as res_mgr, mock.patch(
            'tasks.models.AgentWipCapacity.objects'
        ) as cap_mgr, mock.patch.object(
            WorkflowService, '_release_unconsumed_wip_reservation', side_effect=_fake_release,
        ), mock.patch.object(Status.objects, 'filter', side_effect=_raise):
            qs = mock.Mock()
            qs.select_related.return_value = qs
            qs.select_for_update.return_value = qs
            qs.filter.return_value = qs
            qs.first.return_value = fake_res
            res_mgr.select_related.return_value = qs
            # Also support chained call style used in source
            res_mgr.select_related.return_value.select_for_update.return_value.filter.return_value.first.return_value = fake_res

            cap_mgr.select_for_update.return_value.get.return_value = fake_res.capacity

            with self.assertRaises(_Boom):
                Status.objects.filter(slug='x').first()
            with self.assertRaises(ValidationError) as ctx:
                WorkflowService._verify_wip_reservation(item)

        self.assertIn(CAPACITY_UNAVAILABLE, str(ctx.exception))
        self.assertFalse(released['called'], 'verify must not release on unavailable')

    # ---- caller boundary: routability holds with typed reason ----------------

    def test_seat_routability_records_capacity_unavailable_not_over_capacity(self):
        item = SimpleNamespace(
            pk=4,
            item_key='PLS-737-R',
            mode='task',
            status='needs_triage',
            assignment_group='engineering',
            project=None,
        )
        roster = SimpleNamespace(
            organization_slug='shizuha',
            organization_id=1,
            team_name='engineering',
            active_wip_cap=2,
            ready_wip_cap=4,
            wip_enforced=True,
        )
        with mock.patch.object(
            WorkflowService, '_wip_roster_config', return_value=roster,
        ), mock.patch.object(
            WorkflowService, 'assignable_teams', return_value={'engineering': []},
        ), mock.patch.object(
            WorkflowService, '_canonical_team_name', return_value='engineering',
        ), mock.patch.object(
            WorkflowService, '_task_required_capabilities', return_value=set(),
        ), mock.patch.object(
            WorkflowService, '_seat_satisfies_required_capability', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_is_human_account', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_member_can_execute_from_status', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_merge_sod_candidate_reasons', return_value=[],
        ), mock.patch.object(
            WorkflowService, '_team_roster_entry_for_item', return_value=(None, None),
        ), mock.patch.object(Status.objects, 'filter', side_effect=_raise):
            with self.assertRaises(_Boom):
                Status.objects.filter(slug='x').first()
            verdict = WorkflowService._seat_routability_verdict(
                item, 'engineering', 'nagi@shizuha.com', enforce_cap=True,
            )
        self.assertIn(CAPACITY_UNAVAILABLE, verdict.reasons)
        self.assertNotIn('over_capacity', verdict.reasons)
        self.assertFalse(verdict.ok)


class AlertModeSlotKindTests(TestCase):
    """PLAT-8411: alert-class statuses must classify None (not_applicable)
    before any DB access — alerts are incident episodes, not seat WIP
    (ADR-0056), and are intentionally exempt from the Status table."""

    def setUp(self):
        ws._CAPACITY_CLASSIFIER_DEGRADED.clear()

    def test_full_alert_status_set_classifies_none(self):
        from tasks.models import Item
        for slug, _name in Item.ALERT_STATUS_CHOICES:
            self.assertIsNone(
                WorkflowService._slot_kind_for_status(slug),
                f'alert status {slug!r} must classify None',
            )

    def test_alert_statuses_never_reach_the_status_table(self):
        # Fault the fallible read: the hardcoded exclusion set must fire
        # first, so firing/acknowledged classify None. The exclusion-set
        # builder itself degrades loudly (PLS-737: its own Status read is
        # faulted too) — that is expected. What must NOT happen is the
        # classifier reaching the fallible read: no 'slot_kind_for_status'
        # counter entry proves the hardcoded path decided, not a swallowed
        # fault inside the classification itself.
        with mock.patch.object(Status.objects, 'filter', side_effect=_raise):
            self.assertIsNone(WorkflowService._slot_kind_for_status('firing'))
            self.assertIsNone(WorkflowService._slot_kind_for_status('acknowledged'))
        self.assertNotIn(
            'slot_kind_for_status', ws._CAPACITY_CLASSIFIER_DEGRADED,
            'classifier reached the fallible Status read for an alert status',
        )

    def test_negative_control_non_alert_unknown_status_still_row_absent(self):
        # A slug that is neither hardcoded-excluded nor a working status
        # still reaches the fallible read — under fault it must raise the
        # typed unavailable, not spell a seat.
        with mock.patch.object(Status.objects, 'filter', side_effect=_raise):
            with self.assertRaises(CapacityClassificationUnavailable):
                WorkflowService._slot_kind_for_status('plat8411_unknown_slug')

    def test_task_mode_capacity_semantics_unchanged(self):
        self.assertEqual(WorkflowService._slot_kind_for_status('in_progress'), 'active')
        self.assertEqual(WorkflowService._slot_kind_for_status('todo'), 'ready')
