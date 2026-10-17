"""PLS-408: roster-membership reconciliation in sweep_rebalance_pods.

The team-membership invariant (a concrete AGENT assignee must be a member of
the item's assignment_group) is enforced at the assignee write
(_apply_resolved_assignee) — but items assigned before that guard existed, or
whose owner later left the roster, kept a non-member owner forever. The
2026-07-10 workload-counters handover audit found ~60 such items in active
statuses (e.g. in_review tasks in group `review` still owned by their author
engineer, so no review-team member ever saw them in a queue).

Why the existing ghost-member drain (kei P2) never healed these in prod: it
treats non-roster owners as `down`, and the PLS-204 soft-unavailability grace
then PROTECTS any owner with recent authenticated Pulse activity — so a LIVE
agent holding another team's task keeps it forever. These tests patch
`_soft_unavailable_owner_protected` to True to model exactly that live-owner
state; the PLS-408 pass must reconcile queue-visible items anyway, while
leaving alone: in_progress work (never yank in-flight tasks) and human/operator
pins (PLAT-785). An unstaffed team now fails closed to its visible group queue
instead of retaining an unauthorized concrete owner.
"""
import os
import threading
from datetime import timedelta
from unittest import mock

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from tasks.models import (
    Comment,
    Item,
    OperatorWaitDigestState,
    OperatorWaitMarker,
    Project,
    TeamRoster,
)
from tasks.services.workflow_service import WorkflowService
from .base import create_blocked_item

_SWEEP_ENV = {'PULSE_HEALTH_EXPORTER_URL': '', 'PULSE_API_HEALTH_URL': 'disabled'}

REVIEWER = 'revi@shizuha.com'
ENGINEER = 'ryo@shizuha.com'
GLOBAL_ACCOUNTANT = 'banto@shizuha.com'
PERSONAL_ACCOUNTANT = 'sato@shizuha.com'
PERSONAL_BACKUP = 'mina@shizuha.com'
HUMAN = 'operator@example.test'
ADMIN = 'goro@shizuha.com'
ADMIN_ALIAS = 'goro@agents.shizuha.io'
GORO = ADMIN
SHIZUHA = 'shizuha@shizuha.com'
SHION = 'shion@shizuha.com'

USERS = {
    REVIEWER: 201,
    ENGINEER: 202,
    GLOBAL_ACCOUNTANT: 203,
    PERSONAL_ACCOUNTANT: 204,
    PERSONAL_BACKUP: 205,
    ADMIN: 206,
    SHIZUHA: 207,
    SHION: 208,
}


def _user_by_email(email):
    uid = USERS.get((email or '').strip().lower())
    return {'id': uid, 'email': email} if uid else None


def _organization_by_id(org_id):
    org_id = int(org_id)
    if org_id == 4:
        return {'id': 4, 'slug': 'personal-3', 'name': 'Personal'}
    return {'id': org_id, 'slug': 'shizuha', 'name': 'Shizuha'}


class RosterOwnerReconciliationTests(TestCase):

    def setUp(self):
        self.project = Project.objects.create(
            name='Platform', key='PLAT', org_id=1, access_level='open')
        # Migration 0055/0058 pre-seed a `review` roster — pin our own membership.
        TeamRoster.objects.update_or_create(
            team_name='review',
            defaults={'tiers': [[REVIEWER]], 'junior_emails': [], 'is_active': True})
        TeamRoster.objects.update_or_create(
            team_name='ghost-team',
            defaults={'tiers': [[]], 'junior_emails': [], 'is_active': True})
        TeamRoster.objects.update_or_create(
            team_name='admin-ops',
            defaults={'tiers': [[ADMIN]], 'junior_emails': [], 'is_active': True})
        # _pod_roster caches at class level; reset so rows above are visible.
        WorkflowService._roster_cache = None

    def _mk_task(self, key, status='open', group='review',
                 assignee_email=ENGINEER):
        # NOTE: use statuses valid in the default `simple` workflow — Item.save
        # coerces unknown slugs (e.g. in_review -> in_progress), which would
        # silently change what the sweep sees.
        # These are deliberately pre-guard legacy rows: the test verifies that
        # reconciliation heals a concrete owner who is no longer in the Team.
        # Bypass only the model-level fixture fence; the real sweep still runs
        # with the production organization/team checks enabled.
        with mock.patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            return Item.objects.create(
                project=self.project, item_key=key, title=f'{key} task',
                mode='task', status=status, assignment_group=group,
                assignee_email=assignee_email, assignee_id=USERS.get(assignee_email),
                created_by_email='zen@shizuha.com', created_by_id=0,
                created_at=timezone.now(),
            )

    def _run_sweep(
        self,
        extra_env=None,
        soft_protected=True,
        availability=True,
        max_skew=0,
    ):
        from tasks.tasks import sweep_rebalance_pods
        env = dict(_SWEEP_ENV)
        env.update(extra_env or {})
        with mock.patch('tasks.user_service.user_service.get_user_by_email',
                        side_effect=_user_by_email), \
             mock.patch.object(WorkflowService, '_disabled_agent_emails',
                               return_value=set()), \
             mock.patch.object(WorkflowService, '_is_agent_available_for_assignment',
                               side_effect=(
                                   availability
                                   if callable(availability)
                                   else None
                               ),
                               return_value=availability), \
             mock.patch(
                 'tasks.services.admin_service.admin_service.get_organization',
                 side_effect=_organization_by_id,
             ), \
             mock.patch('tasks.tasks._soft_unavailable_owner_protected',
                        return_value=soft_protected), \
             mock.patch.dict(os.environ, env, clear=False):
            return sweep_rebalance_pods.apply(
                args=[], kwargs={'max_skew': max_skew},
            ).get()

    def test_non_member_live_owner_is_re_leased_to_roster_member(self):
        """The prod escape: a LIVE agent (grace-protected from the ghost drain)
        owning another team's queued task must still be reconciled."""
        item = self._mk_task('PLAT-9401')
        self._run_sweep()
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, REVIEWER)
        self.assertEqual(item.assignment_group, 'review')
        note = Comment.objects.filter(
            item=item, content__contains='PLS-408').first()
        self.assertIsNotNone(note, 'expected a PLS-408 audit comment')
        self.assertIn(ENGINEER, note.content)
        self.assertIn(REVIEWER, note.content)

    def test_roster_member_owner_is_untouched(self):
        item = self._mk_task('PLAT-9402', assignee_email=REVIEWER)
        self._run_sweep()
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, REVIEWER)
        self.assertFalse(
            Comment.objects.filter(item=item, content__contains='PLS-408').exists())

    def test_in_progress_work_is_never_yanked_by_reconciliation(self):
        # The ghost drain is protection-gated off (live owner); the PLS-408
        # pass must not touch in-flight work either.
        item = self._mk_task('PLAT-9403', status='in_progress')
        self._run_sweep()
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, ENGINEER)
        self.assertFalse(
            Comment.objects.filter(item=item, content__contains='PLS-408').exists())

    def test_human_pin_is_respected(self):
        with mock.patch.object(WorkflowService, '_is_human_account',
                               side_effect=lambda e: (e or '').lower() == HUMAN):
            item = self._mk_task('PLAT-9404', assignee_email=HUMAN)
            self._run_sweep()
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, HUMAN)
        self.assertFalse(
            Comment.objects.filter(item=item, content__contains='PLS-408').exists())

    def test_decision_pin_is_respected(self):
        # PLS-233: explicitly direct-assigned (with reason) decision pins must
        # not bounce back to the team pool, even though the owner is a
        # non-member of the group.
        item = self._mk_task('PLAT-9408')
        item.metadata = {'direct_assignment': {
            'assignee_email': ENGINEER, 'pinned_team': 'review'}}
        item.save(update_fields=['metadata'])
        self._run_sweep()
        item.refresh_from_db()
        self.assertEqual(item.assignee_email, ENGINEER)
        self.assertFalse(
            Comment.objects.filter(item=item, content__contains='PLS-408').exists())

    def test_unstaffed_team_clears_unauthorized_owner_to_group_queue(self):
        # ghost-team has an empty roster: retaining a concrete non-member would
        # violate the same organization/team invariant. Keep the task visible
        # on the named Team queue without pretending an owner can execute it.
        item = self._mk_task('PLAT-9405', group='ghost-team')
        self._run_sweep()
        item.refresh_from_db()
        self.assertFalse(item.assignee_email)
        self.assertIsNone(item.assignee_id)
        self.assertEqual(item.assignment_group, 'ghost-team')

    def test_cap_bounds_corrections_per_run(self):
        first = self._mk_task('PLAT-9406')
        second = self._mk_task('PLAT-9407')
        self._run_sweep(extra_env={'PULSE_ROSTER_RECONCILE_CAP': '1'})
        emails = sorted(
            Item.objects.filter(pk__in=[first.pk, second.pk])
            .values_list('assignee_email', flat=True))
        self.assertIn(REVIEWER, emails, 'one item should be re-leased')
        self.assertIn(ENGINEER, emails, 'the other must wait for the next run')

    def test_reconciliation_failure_is_alerted_and_never_reports_success_pls524(self):
        """A swallowed reconciliation exception remains externally unhealthy."""
        from django.core.cache import cache
        from tasks import liveness

        self._mk_task('PLAT-9416')
        cache.delete('pulse:roster_reconciliation:last')
        cache.delete('pulse:roster_reconciliation:last_failure')

        with mock.patch.object(
            WorkflowService,
            '_is_team_member',
            side_effect=RuntimeError('forced roster binding failure'),
        ), mock.patch(
            'tasks.liveness.record_heartbeat',
            return_value=1700000000.0,
        ) as heartbeat, mock.patch(
            'tasks.tasks._notify_roster_reconciliation_owner',
        ) as notify:
            result = self._run_sweep(max_skew=99)

        outcome = result['roster_reconciliation']
        self.assertEqual(outcome['state'], 'failure')
        self.assertIn('forced roster binding failure', outcome['error'])
        self.assertEqual(
            cache.get('pulse:roster_reconciliation:last_failure')['state'],
            'failure',
        )
        self.assertFalse(any(
            call.args == (liveness.ROSTER_RECONCILIATION,)
            for call in heartbeat.call_args_list
        ))
        notify.assert_called_once_with(outcome)

    def test_reconciliation_success_emits_success_heartbeat_pls524(self):
        from tasks import liveness

        self._mk_task('PLAT-9417')
        with mock.patch(
            'tasks.liveness.record_heartbeat',
            return_value=1700000001.0,
        ) as heartbeat:
            result = self._run_sweep(max_skew=99)

        self.assertEqual(result['roster_reconciliation']['state'], 'success')
        heartbeat.assert_any_call(liveness.ROSTER_RECONCILIATION)

    def test_reconciliation_failure_alert_is_deduped_to_engineering_pls524(self):
        from types import SimpleNamespace
        from tasks.tasks import _notify_roster_reconciliation_owner

        alert = SimpleNamespace(id=777)
        payload = {
            'state': 'failure',
            'error': 'forced failure',
            'reconciled': 0,
            'at': '2026-07-18T00:00:00+00:00',
        }
        with mock.patch(
            'tasks.services.deduplication.AlertDeduplicationService.submit_alert',
            side_effect=[(alert, True), (alert, False)],
        ) as submit, mock.patch(
            'tasks.tasks.send_alert_notification.delay',
        ) as notify:
            _notify_roster_reconciliation_owner(payload)
            _notify_roster_reconciliation_owner(payload)

        self.assertEqual(submit.call_count, 2)
        first_alert = submit.call_args_list[0].args[0]
        second_alert = submit.call_args_list[1].args[0]
        self.assertEqual(first_alert['source_id'], second_alert['source_id'])
        self.assertEqual(first_alert['assignment_group'], 'engineering')
        notify.assert_called_once_with(777)

    def test_global_same_named_team_cannot_overwrite_org_local_owner(self):
        """The global accounting sweep must not undo exact-org reconciliation."""
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='accounting',
            defaults={
                'tiers': [[GLOBAL_ACCOUNTANT]],
                'junior_emails': [],
                'is_active': True,
            },
        )
        TeamRoster.objects.update_or_create(
            organization_slug='personal-3',
            team_name='accounting',
            defaults={
                'tiers': [[PERSONAL_ACCOUNTANT]],
                'junior_emails': [],
                'is_active': True,
            },
        )
        personal = Project.objects.create(
            name='Personal Books',
            key='PBK',
            org_id=4,
            access_level='open',
        )
        # Simulate the pre-PLAT-4213 legacy row at fixture construction time.
        # The transaction-level DB proof remains intact; only the model's
        # membership verdict is bypassed for this deliberately corrupt input.
        with mock.patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'personal-3', 'personal-3'),
        ):
            item = Item.objects.create(
                project=personal,
                item_key='PBK-1',
                title='Prepare personal return',
                mode='task',
                status='todo',
                assignment_group='accounting',
                assignee_email=GLOBAL_ACCOUNTANT,
                assignee_id=USERS[GLOBAL_ACCOUNTANT],
                created_by_email='zen@shizuha.com',
                created_by_id=0,
            )
        WorkflowService._roster_cache = None

        self._run_sweep(soft_protected=False)

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, PERSONAL_ACCOUNTANT)
        self.assertTrue(
            Comment.objects.filter(item=item, content__contains='PLS-408').exists()
        )

    def test_accounting_failover_stays_inside_task_organization(self):
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            team_name='accounting',
            defaults={
                'tiers': [[GLOBAL_ACCOUNTANT]],
                'junior_emails': [],
                'is_active': True,
            },
        )
        TeamRoster.objects.update_or_create(
            organization_slug='personal-3',
            team_name='accounting',
            defaults={
                'tiers': [[PERSONAL_ACCOUNTANT, PERSONAL_BACKUP]],
                'junior_emails': [],
                'is_active': True,
            },
        )
        personal = Project.objects.create(
            name='Personal Books',
            key='PBF',
            org_id=4,
            access_level='open',
        )
        item = Item.objects.create(
            project=personal,
            item_key='PBF-1',
            title='Prepare personal return',
            mode='task',
            status='todo',
            assignment_group='accounting',
            assignee_email=PERSONAL_ACCOUNTANT,
            assignee_id=USERS[PERSONAL_ACCOUNTANT],
            created_by_email='zen@shizuha.com',
            created_by_id=0,
        )

        self._run_sweep(
            soft_protected=False,
            availability=lambda email: (
                email.strip().lower() != PERSONAL_ACCOUNTANT
            ),
        )

        item.refresh_from_db()
        self.assertEqual(item.assignee_email, PERSONAL_BACKUP)
        self.assertNotEqual(item.assignee_email, GLOBAL_ACCOUNTANT)

    # PLS-524: the runtime alias and primary address are two representations of
    # one stable subject.  Membership classification must therefore be a no-op
    # across consecutive production sweeps, not a recurring re-lease.
    ALIAS = 'revi@agents.shizuha.io'

    def test_alias_owner_no_reoscillation_across_two_real_sweeps_pls524(self):
        item = self._mk_task('PLAT-9410', assignee_email=self.ALIAS)

        for _ in range(2):
            self._run_sweep()
            item.refresh_from_db()
            self.assertTrue(
                WorkflowService._emails_same_agent_identity(
                    item.assignee_email, REVIEWER,
                )
            )

        self.assertEqual(
            Comment.objects.filter(
                item=item, content__contains='PLS-408',
            ).count(),
            0,
        )

    def test_non_rostered_subject_and_alias_are_never_selected_pls524(self):
        ghost_alias = 'ghost9000@agents.shizuha.io'
        ghost_primary = 'ghost9000@shizuha.com'
        item = self._mk_task('PLAT-9412', assignee_email=ghost_alias)

        for _ in range(2):
            self._run_sweep()
            item.refresh_from_db()
            self.assertNotIn(
                item.assignee_email, {ghost_alias, ghost_primary},
            )
            self.assertEqual(item.assignee_email, REVIEWER)

    def test_binding_snapshot_drives_reconcile_and_write_across_two_sweeps_pls524(self):
        """The rejected subject cannot remain/select from a stale roster.

        The compatibility TeamRoster deliberately contains both seats while the
        armed item-scoped binding contains only REVIEWER.  The real top-level
        sweep must use that same binding for current-owner membership and
        replacement selection on both ticks.
        """
        from tasks.services.routing_authority import AuthorizationDecision

        TeamRoster.objects.update_or_create(
            team_name='review',
            defaults={
                'tiers': [[ENGINEER, REVIEWER]],
                'junior_emails': [],
                'is_active': True,
            },
        )
        WorkflowService._roster_cache = None
        item = self._mk_task('PLAT-9413', assignee_email=ENGINEER)

        def binding(team_name, candidate_item):
            if (candidate_item.pk == item.pk
                    and WorkflowService._canonical_team_name(team_name) == 'review'):
                return {
                    'candidates': [{
                        'user_id': USERS[REVIEWER],
                        'email': REVIEWER,
                        'subject_id': USERS[REVIEWER],
                    }],
                    'reason': '',
                }
            return None

        allowed_decision = AuthorizationDecision(
            allowed=True,
            reason='allowed',
            subject_id=USERS[REVIEWER],
            item_id=item.pk,
            organization_id=1,
            project_id=self.project.pk,
            assignment_group='review',
            workflow_name=item.workflow_name,
        )
        denied_decision = AuthorizationDecision(
            allowed=False,
            reason='subject_not_bound',
            subject_id=USERS[ENGINEER],
            item_id=item.pk,
            organization_id=1,
            project_id=self.project.pk,
            assignment_group='review',
            workflow_name=item.workflow_name,
        )
        authorized_subjects = []

        def authorize(subject_id, *_args, **_kwargs):
            authorized_subjects.append(subject_id)
            if subject_id == USERS[REVIEWER]:
                return allowed_decision
            return denied_decision

        with mock.patch(
            'tasks.services.routing_authority.binding_candidates_for_item',
            side_effect=binding,
        ), mock.patch(
            'tasks.services.routing_authority.subject_authorized_for_item',
            side_effect=authorize,
        ), mock.patch(
            'tasks.services.routing_authority.routing_authority_applies_to_context',
            return_value=False,
        ), mock.patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            self.assertFalse(
                WorkflowService._is_team_member(
                    ENGINEER, 'review', item=item,
                )
            )
            self.assertEqual(
                WorkflowService._resolve_team_assignee(
                    'review', item=item,
                )[1],
                REVIEWER,
            )
            for _ in range(2):
                self._run_sweep(max_skew=99)
                item.refresh_from_db()
                self.assertEqual(item.assignee_email, REVIEWER)

        self.assertTrue(authorized_subjects)
        self.assertEqual(
            set(authorized_subjects),
            {USERS[REVIEWER]},
            'every resolver/save fence must authorize only the replacement',
        )
        self.assertEqual(
            Comment.objects.filter(
                item=item, content__contains='PLS-408',
            ).count(),
            1,
        )

    def test_eligible_available_linked_blocked_owner_is_held_pls524(self):
        with mock.patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            item = create_blocked_item(
                project=self.project,
                item_key='PLAT-9414',
                title='available blocked owner remains stable',
                mode='task',
                assignment_group='review',
                assignee_email=REVIEWER,
                assignee_id=USERS[REVIEWER],
                created_by_email='zen@shizuha.com',
                created_by_id=0,
                workflow_name='autonomous-dev',
            )

        for _ in range(2):
            self._run_sweep()
            item.refresh_from_db()
            self.assertEqual(item.assignee_email, REVIEWER)

        self.assertFalse(
            Comment.objects.filter(
                item=item, content__contains='PLS-408',
            ).exists()
        )

    def test_ineligible_available_linked_blocked_owner_clears_pls524(self):
        with mock.patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            item = create_blocked_item(
                project=self.project,
                item_key='PLAT-9415',
                title='ineligible blocked owner returns to group queue',
                mode='task',
                assignment_group='review',
                assignee_email=ENGINEER,
                assignee_id=USERS[ENGINEER],
                created_by_email='zen@shizuha.com',
                created_by_id=0,
                workflow_name='autonomous-dev',
            )

        for _ in range(2):
            self._run_sweep()
            item.refresh_from_db()
            self.assertIsNone(item.assignee_email)
            self.assertIsNone(item.assignee_id)
            self.assertEqual(item.assignment_group, 'review')
        note = Comment.objects.filter(
            item=item, content__contains='Cleared ineligible concrete owner',
        )
        self.assertEqual(note.count(), 1)
        self.assertIn(ENGINEER, note.get().content)

    def test_concurrent_swap_to_valid_owner_before_clear_is_preserved_pls524(self):
        """PLS-524 P1 write-fence race (revi review of PR #41).

        A stale rejected owner is decided ineligible on the UNLOCKED pre-filter
        snapshot, but the clear/replace writes under a LATER row lock. If a
        workflow transition / blocker auto-resume / concurrent reassignment
        replaces that owner with a VALID roster member in between, the write must
        NOT strand the newly-valid owner. The fix revalidates every gate under
        the same ``select_for_update`` lock as the write, so this test — which
        swaps the owner to a member immediately before the locked repair — proves
        the valid owner survives. Before the fix, the blocked-clear command
        re-snapshots the owner at lock time and clears REVIEWER; this fails.
        """
        with mock.patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            item = create_blocked_item(
                project=self.project,
                item_key='PLAT-9421',
                title='concurrent valid owner survives the write fence',
                mode='task',
                assignment_group='review',
                assignee_email=ENGINEER,  # stale non-member the pre-filter rejects
                assignee_id=USERS[ENGINEER],
                created_by_email='zen@shizuha.com',
                created_by_id=0,
                workflow_name='autonomous-dev',
            )

        orig_op = WorkflowService._is_operator_target
        swapped = []

        def _swap_then_check(assignee_id, email):
            # Simulate a concurrent reassignment to a VALID member landing AFTER
            # the unlocked pre-filter read but BEFORE the locked clear.
            if not swapped and (email or '').strip().lower() == ENGINEER:
                Item.objects.filter(pk=item.pk).update(
                    assignee_email=REVIEWER, assignee_id=USERS[REVIEWER])
                swapped.append(True)
            return orig_op(assignee_id, email)

        with mock.patch.object(
            WorkflowService, '_is_operator_target',
            side_effect=_swap_then_check,
        ):
            self._run_sweep()

        self.assertTrue(swapped, 'the pre-filter swap hook must have fired')
        item.refresh_from_db()
        # REVIEWER is a member of `review`: the locked revalidation must re-read
        # the current owner and SKIP — the newly-valid owner is not cleared.
        self.assertEqual(item.assignee_email, REVIEWER)
        self.assertEqual(item.assignee_id, USERS[REVIEWER])
        self.assertEqual(item.assignment_group, 'review')
        self.assertFalse(
            Comment.objects.filter(
                item=item, content__contains='PLS-524',
            ).exists(),
            'a valid concurrent owner must not be cleared to the group queue',
        )

    def test_recurring_due_fire_lease_and_completion_are_one_generation_pls524(self):
        """Real due-fire -> two sweeps -> completion, including stale replay.

        This is the production caller order that the original PR fixture
        skipped.  One overdue weekly boundary opens one generation, keeps one
        canonical admin-ops owner, records one completion watermark, and parks
        at exactly the consumed boundary plus seven days.
        """
        from datetime import timedelta
        from tasks.services.recurring_cycle_close import (
            close_recurring_cycle_if_complete,
        )
        from tasks.services.recurring_cycle_state import (
            cycle_snapshot,
            fire_due_cycle,
        )

        old_due = timezone.now() - timedelta(hours=1)
        item = Item.objects.create(
            project=self.project,
            item_key='EVOL-8T',
            title='weekly council cycle',
            mode='task',
            status='scheduled',
            assignment_group='admin-ops',
            assignee_email=ADMIN_ALIAS,
            assignee_id=USERS[ADMIN],
            created_by_email='zen@shizuha.com',
            created_by_id=0,
            workflow_name='autonomous-dev',
            workflow_status='Scheduled',
            is_recurring=True,
            frequency='weekly',
            due_date=old_due,
        )
        initial = cycle_snapshot(item)

        fired = fire_due_cycle(
            item.pk,
            expected_cycle_revision=initial['cycle_revision'],
            expected_generation=initial['generation'],
            expected_due_at=initial['due_boundary'],
            expected_completion_watermark=(
                initial['completion_watermark'] or ''
            ),
        )
        self.assertTrue(fired.applied)
        self.assertEqual(fired.generation, 1)
        self.assertEqual(fired.next_due, old_due + timedelta(days=7))

        for _ in range(2):
            self._run_sweep()
            item.refresh_from_db()
            self.assertTrue(
                WorkflowService._emails_same_agent_identity(
                    item.assignee_email, ADMIN,
                )
            )

        Comment.objects.create(
            item=item,
            author_id=USERS[ADMIN],
            author_email=ADMIN,
            content='[recurring-cycle complete] weekly council published',
        )
        closed = close_recurring_cycle_if_complete(
            item,
            trigger_kind='task',
            trigger_label='EVOL-8 weekly session',
            actor_email=ADMIN,
        )
        self.assertTrue(closed.closed)

        item.refresh_from_db()
        cycle = item.metadata['recurring_cycle']
        self.assertEqual(item.status, 'scheduled')
        self.assertEqual(item.due_date, old_due + timedelta(days=7))
        self.assertEqual(cycle['active_generation'], 1)
        self.assertEqual(cycle['completed_generation'], 1)
        self.assertTrue(cycle['last_terminal_completion_at'])
        watermark = cycle['last_terminal_completion_at']

        duplicate_close = close_recurring_cycle_if_complete(
            item,
            trigger_kind='task',
            trigger_label='EVOL-8 weekly session',
            actor_email=ADMIN,
        )
        self.assertFalse(duplicate_close.closed)
        replay = fire_due_cycle(
            item.pk,
            expected_cycle_revision=initial['cycle_revision'],
            expected_generation=initial['generation'],
            expected_due_at=initial['due_boundary'],
            expected_completion_watermark=(
                initial['completion_watermark'] or ''
            ),
        )
        self.assertFalse(replay.applied)
        item.refresh_from_db()
        self.assertEqual(
            item.metadata['recurring_cycle']['last_terminal_completion_at'],
            watermark,
        )
        self.assertEqual(
            Comment.objects.filter(
                item=item,
                content__contains='[recurring-cycle auto-close]',
            ).count(),
            1,
        )
class OperatorWaitFailoverSequenceTests(TestCase):
    """PLS-529 production order: digest → wait → mutation → one lease.

    This mirrors the real sweep entry point rather than repeatedly invoking the
    marker helper: two surfaced operator roots sit on an unavailable owner,
    consecutive failover/rebalance intervals run, evidence mutates one root,
    then the same top-level sweep runs twice again.
    """

    def setUp(self):
        self.project = Project.objects.create(
            name='Operator wait', key='OPW', org_id=1, access_level='open')
        self.digest_project = Project.objects.create(
            name='Evolution', key='EVOL', org_id=1, access_level='open')
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            organization_id=1,
            team_name='admin-ops',
            defaults={
                'tiers': [[GORO, SHIZUHA, SHION]],
                'junior_emails': [],
                'is_active': True,
            },
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            organization_id=1,
            team_name='engineering',
            defaults={
                'tiers': [[ENGINEER]],
                'junior_emails': [],
                'is_active': True,
            },
        )
        WorkflowService._roster_cache = None
        self.digest = Item.objects.create(
            project=self.digest_project,
            item_key='EVOL-36',
            title='Blocked-on-operator digest',
            mode='task',
            workflow_name='autonomous-dev',
            status='scheduled',
            assignment_group='admin-ops',
            assignee_id=USERS[SHION],
            assignee_email=SHION,
        )

    def _parent_and_root(self, suffix, *, group='admin-ops', due_date=None):
        root = Item.objects.create(
            project=self.project,
            item_key=f'OPW-{int(suffix) + 20}',
            title=f'Canonical blocker {suffix}',
            mode='task',
            workflow_name='autonomous-dev',
            status='open',
            assignment_group=group,
            assignee_id=USERS[GORO] if group == 'admin-ops' else USERS[ENGINEER],
            assignee_email=GORO if group == 'admin-ops' else ENGINEER,
            tags='blocker,decision-required,escalation',
        )
        parent = create_blocked_item(
            blocker=root,
            project=self.project,
            item_key=f'OPW-{suffix}',
            title=f'Operator dependency {suffix}',
            mode='task',
            workflow_name='autonomous-dev',
            assignment_group='admin-ops',
            assignee_id=USERS[SHION],
            assignee_email=SHION,
            due_date=due_date,
            metadata={'escalation_routing_gate': {
                'classification': 'genuine_decision_or_physical_action',
                'outcome': 'allowed',
            }},
        )
        parent.refresh_from_db()
        self.assertEqual(parent.status, 'blocked')
        return parent, root

    def _ack(self, *keys):
        return Comment.objects.create(
            item=self.digest,
            author_id=USERS[SHION],
            author_email=SHION,
            content=(
                '## Blocked-on-operator digest — fixture\n\n'
                'DM delivered to Hritik via Connect '
                '(message_id `b7d441c6-1805-4ab7-bfa3-fdba5c86bc1d`).\n\n'
                + '\n'.join(f'- **{key}** — operator evidence required.' for key in keys)
            ),
        )

    def _run_sweep(self, *available_emails):
        from tasks.tasks import sweep_rebalance_pods

        def available(email):
            return (email or '').strip().lower() in {
                value.strip().lower() for value in available_emails
            }

        with mock.patch(
            'tasks.user_service.user_service.get_user_by_email',
            side_effect=_user_by_email,
        ), mock.patch.object(
            WorkflowService, '_disabled_agent_emails', return_value=set(),
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            side_effect=available,
        ), mock.patch(
            'tasks.services.admin_service.admin_service.get_organization',
            side_effect=_organization_by_id,
        ), mock.patch(
            'tasks.tasks._soft_unavailable_owner_protected', return_value=False,
        ), mock.patch.dict(
            os.environ,
            {**_SWEEP_ENV, 'PULSE_FAILOVER_COOLDOWN_HOURS': '0'},
            clear=False,
        ):
            return sweep_rebalance_pods.apply(
                args=[], kwargs={'max_skew': 0},
            ).get()

    def test_real_caller_sequence_suppresses_then_rearms_exactly_once(self):
        first_parent, first = self._parent_and_root(1)
        second_parent, second = self._parent_and_root(2)
        team_parent, team_root = self._parent_and_root(3, group='engineering')

        # Reproduce the live goro → shizuha → shion caller order first.
        self._run_sweep(SHIZUHA)
        self._run_sweep(SHION)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.assignee_email, SHION)
        self.assertEqual(second.assignee_email, SHION)
        pre_wait_failovers = {
            item.pk: Comment.objects.filter(
                item=item,
                author_email='system@pulse',
                content__startswith='Auto-failover',
            ).count()
            for item in (first, second)
        }
        self.assertEqual(pre_wait_failovers, {first.pk: 2, second.pk: 2})

        lower = Item.objects.create(
            project=self.project,
            item_key='OPW-40',
            title='Ordinary owner work',
            mode='task',
            workflow_name='autonomous-dev',
            status='open',
            assignment_group='admin-ops',
            assignee_id=USERS[SHION],
            assignee_email=SHION,
        )

        self._ack(
            first_parent.item_key,
            second_parent.item_key,
            team_parent.item_key,
        )

        self.assertEqual(
            set(OperatorWaitMarker.objects.filter(state='active').values_list(
                'item_id', flat=True,
            )),
            {first.pk, second.pk},
        )
        self.assertIsNone(
            WorkflowService._higher_impact_owned_blocker(lower, SHION),
            'operator_wait roots must not become reciprocal focus predecessors',
        )
        self.assertFalse(
            OperatorWaitMarker.objects.filter(item=team_root).exists(),
            'team-doable blockers must never receive operator_wait',
        )

        self._run_sweep(GORO)
        self._run_sweep(GORO)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.assignee_email, SHION)
        self.assertEqual(second.assignee_email, SHION)
        self.assertEqual({
            item.pk: Comment.objects.filter(
                item=item,
                author_email='system@pulse',
                content__startswith='Auto-failover',
            ).count()
            for item in (first, second)
        }, pre_wait_failovers)

        Comment.objects.create(
            item=first,
            author_id=USERS[SHION],
            author_email=SHION,
            content='New operator evidence arrived; re-evaluate this root.',
        )
        first.operator_wait_marker.refresh_from_db()
        self.assertEqual(first.operator_wait_marker.state, 'inactive')

        self._run_sweep(SHIZUHA)
        self._run_sweep(SHIZUHA)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.assignee_email, SHIZUHA)
        self.assertEqual(second.assignee_email, SHION)
        self.assertEqual(Comment.objects.filter(
            item=first,
            author_email='system@pulse',
            content__startswith='Auto-failover',
        ).count(), pre_wait_failovers[first.pk] + 1)

    def test_digest_withdrawal_and_deadline_event_release_wait(self):
        parent, root = self._parent_and_root(4)
        ack = self._ack(parent.item_key)
        marker = OperatorWaitMarker.objects.get(item=root)
        self.assertEqual(marker.state, 'active')

        ack.delete()
        marker.refresh_from_db()
        self.assertEqual(marker.state, 'inactive')

        past_parent, past_root = self._parent_and_root(
            5, due_date=timezone.now() - timedelta(seconds=1),
        )
        self._ack(past_parent.item_key)
        self.assertEqual(
            OperatorWaitMarker.objects.get(item=past_root).state,
            'inactive',
        )

    def test_editing_older_digest_cannot_regress_high_water(self):
        first_parent, first_root = self._parent_and_root(7)
        second_parent, second_root = self._parent_and_root(8)
        older = self._ack(first_parent.item_key)
        newer = self._ack(second_parent.item_key)

        first_root.operator_wait_marker.refresh_from_db()
        self.assertEqual(first_root.operator_wait_marker.state, 'inactive')
        self.assertEqual(
            OperatorWaitMarker.objects.get(item=second_root).state,
            'active',
        )

        older.content += '\n\nEdited after the replacement digest landed.'
        older.save(update_fields=['content'])

        state = OperatorWaitDigestState.objects.get(digest_item=self.digest)
        self.assertEqual(state.source_comment_id, newer.pk)
        self.assertEqual(state.source_comment_order, newer.pk)
        first_root.operator_wait_marker.refresh_from_db()
        self.assertEqual(first_root.operator_wait_marker.state, 'inactive')
        self.assertEqual(
            OperatorWaitMarker.objects.get(item=second_root).state,
            'active',
        )

    def test_deleting_identical_older_digest_keeps_newer_high_water(self):
        parent, root = self._parent_and_root(9)
        older = self._ack(parent.item_key)
        newer = self._ack(parent.item_key)
        self.assertEqual(older.content, newer.content)

        older.delete()

        state = OperatorWaitDigestState.objects.get(digest_item=self.digest)
        self.assertEqual(state.state, 'active')
        self.assertEqual(state.source_comment_id, newer.pk)
        self.assertEqual(state.source_comment_order, newer.pk)
        self.assertEqual(
            OperatorWaitMarker.objects.get(item=root).state,
            'active',
        )

    def test_digest_cannot_classify_another_organizations_operator_root(self):
        TeamRoster.objects.update_or_create(
            organization_slug='personal-3',
            organization_id=4,
            team_name='admin-ops',
            defaults={
                'tiers': [[GORO]],
                'junior_emails': [],
                'is_active': True,
            },
        )
        WorkflowService._roster_cache = None
        foreign_project = Project.objects.create(
            name='Foreign operator wait', key='FOW', org_id=4,
            access_level='open',
        )
        foreign_root = Item.objects.create(
            project=foreign_project,
            item_key='FOW-2',
            title='Foreign canonical blocker',
            mode='task',
            workflow_name='autonomous-dev',
            status='open',
            assignment_group='admin-ops',
            assignee_id=USERS[GORO],
            assignee_email=GORO,
            tags='blocker,decision-required,escalation',
        )
        foreign_parent = create_blocked_item(
            blocker=foreign_root,
            project=foreign_project,
            item_key='FOW-1',
            title='Foreign operator dependency',
            mode='task',
            workflow_name='autonomous-dev',
            assignment_group='admin-ops',
            assignee_id=USERS[GORO],
            assignee_email=GORO,
            metadata={'escalation_routing_gate': {
                'classification': 'genuine_decision_or_physical_action',
                'outcome': 'allowed',
            }},
        )

        self._ack(foreign_parent.item_key)

        self.assertFalse(
            OperatorWaitMarker.objects.filter(item=foreign_root).exists(),
            'a digest author without org-4 authority must not control its root',
        )

    def test_authorized_cross_org_digest_suppresses_repeated_failover(self):
        TeamRoster.objects.update_or_create(
            organization_slug='personal-3',
            organization_id=4,
            team_name='admin-ops',
            defaults={
                'tiers': [[GORO, SHIZUHA, SHION]],
                'junior_emails': [],
                'is_active': True,
            },
        )
        WorkflowService._roster_cache = None
        foreign_project = Project.objects.create(
            name='Personal operator wait', key='BKSH', org_id=4,
            access_level='open',
        )
        foreign_root = Item.objects.create(
            project=foreign_project,
            item_key='BKSH-8',
            title='Personal canonical blocker',
            mode='task',
            workflow_name='autonomous-dev',
            status='open',
            assignment_group='admin-ops',
            assignee_id=USERS[GORO],
            assignee_email=GORO,
            tags='blocker,decision-required,escalation',
        )
        foreign_parent = create_blocked_item(
            blocker=foreign_root,
            project=foreign_project,
            item_key='BKSH-7',
            title='Personal operator dependency',
            mode='task',
            workflow_name='autonomous-dev',
            assignment_group='admin-ops',
            assignee_id=USERS[SHION],
            assignee_email=SHION,
            metadata={'escalation_routing_gate': {
                'classification': 'genuine_decision_or_physical_action',
                'outcome': 'allowed',
            }},
        )

        # Production order: Goro fails over twice before the global digest
        # surfaces the personal-org ask, then availability churn must stop.
        self._run_sweep(SHIZUHA)
        self._run_sweep(SHION)
        foreign_root.refresh_from_db()
        self.assertEqual(foreign_root.assignee_email, SHION)
        failovers_before_wait = Comment.objects.filter(
            item=foreign_root,
            author_email='system@pulse',
            content__startswith='Auto-failover',
        ).count()
        self.assertEqual(failovers_before_wait, 2)

        self._ack(foreign_parent.item_key)
        marker = OperatorWaitMarker.objects.get(item=foreign_root)
        self.assertEqual(marker.state, 'active')
        self.assertEqual(marker.surfaced_item, foreign_parent)

        self._run_sweep(GORO)
        self._run_sweep(GORO)
        foreign_root.refresh_from_db()
        self.assertEqual(foreign_root.assignee_email, SHION)
        self.assertEqual(Comment.objects.filter(
            item=foreign_root,
            author_email='system@pulse',
            content__startswith='Auto-failover',
        ).count(), failovers_before_wait)

    def test_top_level_reconciliation_records_success_liveness(self):
        from tasks import liveness

        with mock.patch(
            'tasks.liveness.record_heartbeat', return_value=1234.0,
        ) as heartbeat:
            self._run_sweep(SHION)

        heartbeat.assert_any_call(liveness.OPERATOR_WAIT)

    def test_top_level_reconciliation_failure_uses_fail_loud_path(self):
        with mock.patch(
            'tasks.services.operator_wait.reconcile_operator_wait_markers',
            side_effect=RuntimeError('digest parser broke'),
        ), mock.patch(
            'tasks.services.operator_wait.report_operator_wait_failure',
        ) as fail_loud:
            self._run_sweep(SHION)

        fail_loud.assert_called_once()
        self.assertEqual(fail_loud.call_args.args[0], 'sweep_rebalance_pods')
        self.assertIsInstance(fail_loud.call_args.args[1], RuntimeError)

    def test_repeated_reconciliation_failures_do_not_refresh_success_liveness(self):
        from tasks import liveness

        with mock.patch(
            'tasks.services.operator_wait.reconcile_operator_wait_markers',
            side_effect=RuntimeError('digest parser broke'),
        ), mock.patch(
            'tasks.liveness.record_heartbeat',
        ) as heartbeat, mock.patch(
            'tasks.services.operator_wait.cache.set',
        ) as cache_set, mock.patch(
            'tasks.services.operator_wait.cache.add', return_value=False,
        ):
            self._run_sweep(SHION)
            self._run_sweep(SHION)

        operator_wait_heartbeats = [
            call for call in heartbeat.call_args_list
            if call.args and call.args[0] == liveness.OPERATOR_WAIT
        ]
        self.assertEqual(operator_wait_heartbeats, [])
        failure_markers = [
            call for call in cache_set.call_args_list
            if call.args and call.args[0] == 'pulse:operator_wait:last_failure'
        ]
        self.assertEqual(len(failure_markers), 2)
        self.assertTrue(all(
            call.args[1]['state'] == 'failure' for call in failure_markers
        ))

    def test_roster_resolution_failure_fails_loud_across_two_sweeps(self):
        from tasks import liveness

        parent, _root = self._parent_and_root(6)
        self._ack(parent.item_key)
        response = mock.Mock(status_code=201)
        dedupe_keys = set()

        def add_once(key, *_args, **_kwargs):
            if key in dedupe_keys:
                return False
            dedupe_keys.add(key)
            return True

        with mock.patch.object(
            WorkflowService,
            '_team_roster_entry_for_item',
            side_effect=RuntimeError('admin-ops roster unavailable'),
        ), mock.patch.object(
            WorkflowService,
            '_resolve_team_lead_email',
            return_value=ENGINEER,
        ), mock.patch(
            'tasks.liveness.record_heartbeat',
        ) as heartbeat, mock.patch(
            'tasks.services.operator_wait.cache.set',
        ) as cache_set, mock.patch(
            'tasks.services.operator_wait.cache.add',
            side_effect=add_once,
        ), mock.patch(
            'tasks.services.operator_wait.requests.post',
            return_value=response,
        ) as post, mock.patch(
            'tasks.services.notifications.UserNotificationService.notify_user',
        ) as notify:
            self._run_sweep(SHION)
            self._run_sweep(SHION)

        self.assertFalse(any(
            call.args and call.args[0] == liveness.OPERATOR_WAIT
            for call in heartbeat.call_args_list
        ))
        failure_markers = [
            call for call in cache_set.call_args_list
            if call.args and call.args[0] == 'pulse:operator_wait:last_failure'
        ]
        self.assertEqual(len(failure_markers), 2)
        self.assertTrue(all(
            'admin-ops roster unavailable' in call.args[1]['error']
            for call in failure_markers
        ))
        post.assert_called_once()
        notify.assert_called_once()

    def test_fail_loud_path_dms_engineering_owner(self):
        from tasks.services.operator_wait import report_operator_wait_failure

        response = mock.Mock(status_code=201)
        with mock.patch(
            'tasks.services.workflow_service.WorkflowService._resolve_team_lead_email',
            return_value='kai@shizuha.com',
        ), mock.patch(
            'tasks.user_service.user_service.get_user_by_email',
            return_value={'id': 209, 'email': 'kai@shizuha.com'},
        ), mock.patch(
            'tasks.services.operator_wait.requests.post', return_value=response,
        ) as post, mock.patch(
            'tasks.services.operator_wait.cache.add', return_value=True,
        ), mock.patch(
            'tasks.services.notifications.UserNotificationService.notify_user',
        ) as notify:
            payload = report_operator_wait_failure(
                'comment_edit', RuntimeError('digest parser broke'), target=99,
            )

        self.assertEqual(payload['state'], 'failure')
        body = post.call_args.kwargs['json']
        self.assertEqual(body['recipient_user_id'], 209)
        self.assertIn('Engineering / `kai@shizuha.com`', body['content'])
        self.assertIn('digest parser broke target=99', body['content'])
        notify.assert_called_once()


class OperatorWaitReconciliationConcurrencyTests(TransactionTestCase):
    """PostgreSQL proof: signal + sweep serialize on the digest cursor."""

    reset_sequences = True

    def setUp(self):
        self.project = Project.objects.create(
            name='Operator wait concurrency', key='OWC', org_id=1,
            access_level='open',
        )
        digest_project = Project.objects.create(
            name='Evolution concurrency', key='EVC', org_id=1,
            access_level='open',
        )
        TeamRoster.objects.update_or_create(
            organization_slug='shizuha',
            organization_id=1,
            team_name='admin-ops',
            defaults={
                'tiers': [[GORO, SHION]],
                'junior_emails': [],
                'is_active': True,
            },
        )
        WorkflowService._roster_cache = None
        self.digest = Item.objects.create(
            project=digest_project,
            item_key='EVOL-36',
            title='Blocked-on-operator digest concurrency',
            mode='task',
            workflow_name='autonomous-dev',
            status='scheduled',
            assignment_group='admin-ops',
            assignee_id=USERS[SHION],
            assignee_email=SHION,
        )
        self.root = Item.objects.create(
            project=self.project,
            item_key='OWC-2',
            title='Concurrent canonical blocker',
            mode='task',
            workflow_name='autonomous-dev',
            status='open',
            assignment_group='admin-ops',
            assignee_id=USERS[SHION],
            assignee_email=SHION,
            tags='blocker,decision-required,escalation',
        )
        self.parent = create_blocked_item(
            blocker=self.root,
            project=self.project,
            item_key='OWC-1',
            title='Concurrent operator dependency',
            mode='task',
            workflow_name='autonomous-dev',
            assignment_group='admin-ops',
            assignee_id=USERS[SHION],
            assignee_email=SHION,
            metadata={'escalation_routing_gate': {
                'classification': 'genuine_decision_or_physical_action',
                'outcome': 'allowed',
            }},
        )
        with mock.patch(
            'tasks.services.operator_wait.reconcile_operator_wait_markers',
        ):
            self.ack = Comment.objects.create(
                item=self.digest,
                author_id=USERS[SHION],
                author_email=SHION,
                content=(
                    '## Blocked-on-operator digest — concurrency fixture\n\n'
                    'DM delivered to Hritik via Connect '
                    '(message_id `9f6ba27f-e105-4e97-a0cd-e24653caed9a`).\n\n'
                    f'- **{self.parent.item_key}** — operator evidence required.'
                ),
            )

    @mock.patch(
        'tasks.services.notifications.UserNotificationService.notify_user',
    )
    def test_signal_and_sweep_create_one_marker_without_duplicate_effects(
        self, notify,
    ):
        if connection.vendor != 'postgresql':
            self.skipTest('row-lock concurrency requires PostgreSQL')

        from tasks.services.operator_wait import (
            handle_digest_comment_saved,
        )
        from tasks.tasks import sweep_rebalance_pods

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker(source):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                if source == 'signal':
                    comment = Comment.objects.get(pk=self.ack.pk)
                    result = handle_digest_comment_saved(comment)
                else:
                    result = sweep_rebalance_pods.apply(
                        args=[], kwargs={'max_skew': 0},
                    ).get()
                results.append(result)
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        with mock.patch(
            'tasks.user_service.user_service.get_user_by_email',
            side_effect=_user_by_email,
        ), mock.patch.object(
            WorkflowService, '_disabled_agent_emails', return_value=set(),
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            side_effect=lambda email: (email or '').strip().lower() == GORO,
        ), mock.patch(
            'tasks.services.admin_service.admin_service.get_organization',
            side_effect=_organization_by_id,
        ), mock.patch(
            'tasks.tasks._soft_unavailable_owner_protected', return_value=False,
        ), mock.patch.dict(
            os.environ,
            {**_SWEEP_ENV, 'PULSE_FAILOVER_COOLDOWN_HOURS': '0'},
            clear=False,
        ):
            threads = [
                threading.Thread(target=worker, args=('signal',)),
                threading.Thread(target=worker, args=('sweep',)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(OperatorWaitDigestState.objects.count(), 1)
        self.assertEqual(OperatorWaitMarker.objects.count(), 1)
        marker = OperatorWaitMarker.objects.get(item=self.root)
        self.assertEqual(marker.state, 'active')
        self.assertEqual(marker.generation, 1)
        self.assertFalse(Comment.objects.filter(
            item=self.root,
            author_email='system@pulse',
            content__startswith='Auto-failover',
        ).exists())
        self.assertFalse(any(
            call.kwargs.get('item') == self.root
            for call in notify.call_args_list
        ))
