"""Runnable dependency roots are always visible, admitted, and focus-pinned."""

from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase

from tasks.models import ExternalWaitAuthority, Item, ItemLink, Project, Resolution
from tasks.services.actionable_items import filter_active_queue_items
from tasks.services.workflow_service import (
    WorkflowService,
    WorkflowValidationError,
)


class BlockerWipInvariantTests(TestCase):
    EMAIL = 'blocker-owner@example.com'
    UID = 7710

    def _item(self, title, status='open', priority='normal', **kwargs):
        return Item.objects.create(
            title=title,
            mode='task',
            status=status,
            priority=priority,
            workflow_name='autonomous-dev',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            **kwargs,
        )

    def _link_without_auto_block(self, blocker, dependent):
        link = ItemLink(
            source_item=blocker,
            target_item=dependent,
            link_type='blocks',
            created_by_id=self.UID,
            created_by_email=self.EMAIL,
        )
        link._skip_auto_block = True
        link.save()
        return link

    def test_link_event_promotes_backlog_root_and_blocks_descendant(self):
        root = self._item('parked root', status='backlog', priority='normal')
        dependent = self._item('dependent', priority='urgent')

        self._link_without_auto_block(root, dependent)
        dependent.status = 'blocked'
        dependent.save(update_fields=['status'])
        root.refresh_from_db()
        dependent.refresh_from_db()

        self.assertNotEqual(root.status, 'backlog')
        # Operator directive 2026-08-08: a task that blocks active work must be
        # carried at the highest priority so the rebalancer assigns it first.
        self.assertEqual(root.priority, 'urgent')
        self.assertEqual(dependent.status, 'blocked')
        self.assertEqual(
            list(
                filter_active_queue_items(
                    Item.objects.filter(pk=root.pk)
                ).values_list('pk', flat=True)
            ),
            [root.pk],
        )

    def test_legacy_backlog_root_is_visible_until_event_repair(self):
        root = self._item('legacy parked root', status='backlog')
        dependent = self._item('legacy dependent', priority='urgent')
        ItemLink.objects.bulk_create([ItemLink(
            source_item=root,
            target_item=dependent,
            link_type='blocks',
            created_by_id=self.UID,
            created_by_email=self.EMAIL,
        )])
        dependent.status = 'blocked'
        dependent.save(update_fields=['status'])
        root.refresh_from_db()

        self.assertEqual(root.status, 'backlog')
        self.assertTrue(
            filter_active_queue_items(
                Item.objects.filter(pk=root.pk)
            ).exists()
        )
        # The database constraint is deferred so repair and link creation can
        # be atomic. Leave the transaction valid after exercising the
        # fail-safe visibility path used while that repair is in flight.
        Item.objects.filter(pk=root.pk).update(
            status='open',
            workflow_status='open',
        )

    def test_active_blocker_cannot_be_demoted_to_backlog(self):
        root = self._item('active root')
        dependent = self._item('blocked dependent', priority='urgent')
        self._link_without_auto_block(root, dependent)

        root.status = 'backlog'
        with self.assertRaises(ValidationError):
            root.save(update_fields=['status'])
        root.refresh_from_db()
        self.assertEqual(root.status, 'open')

    def test_scheduled_blocker_is_promoted_and_descendant_stays_blocked(self):
        root = self._item('scheduled root', status='scheduled')
        dependent = self._item('scheduled root dependent', priority='urgent')

        self._link_without_auto_block(root, dependent)
        dependent.status = 'blocked'
        dependent.save(update_fields=['status'])

        root.refresh_from_db()
        dependent.refresh_from_db()
        self.assertNotIn(root.status, {'backlog', 'scheduled'})
        self.assertEqual(dependent.status, 'blocked')

    def test_active_blocker_cannot_be_demoted_to_scheduled(self):
        root = self._item('scheduled demotion root')
        dependent = self._item('scheduled demotion dependent')
        self._link_without_auto_block(root, dependent)

        root.status = 'scheduled'
        with self.assertRaises(ValidationError):
            root.save(update_fields=['status'])
        root.refresh_from_db()
        self.assertEqual(root.status, 'open')

    def test_lower_task_start_is_rejected_while_owned_root_exists(self):
        root = self._item('owned release root', priority='urgent')
        dependent = self._item('blocked descendant', priority='urgent')
        self._link_without_auto_block(root, dependent)
        dependent.status = 'blocked'
        dependent.save(update_fields=['status'])
        lower = self._item('lower impact task', priority='normal')

        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService.validate_transition(
                lower,
                'in_progress',
                user=None,
                screen_data={},
        )
        self.assertEqual(caught.exception.code, 'BLOCKER_FOCUS_REQUIRED')
        self.assertIn(root.item_key or str(root.pk), str(caught.exception))

    def test_active_wip_occupant_can_progress_when_ready_root_cannot_start(self):
        """Focus and active-cap admission cannot form a two-guard deadlock."""
        project = Project.objects.create(
            name='PLS-826 coherent WIP', key='P826W', org_id=826,
        )
        with mock.patch.object(
            WorkflowService,
            'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            root = self._item(
                'resume guard root', priority='urgent', project=project,
                assignment_group='engineering',
            )
            dependent = self._item(
                'resume guard descendant', priority='urgent', project=project,
            )
            self._link_without_auto_block(root, dependent)
            occupant = self._item(
                'already active lower task',
                status='in_progress',
                priority='normal',
                project=project,
                assignment_group='engineering',
            )
            other = self._item(
                'other ready task', priority='normal', project=project,
                assignment_group='engineering',
            )
        config = SimpleNamespace(
            organization_slug='shizuha', active_wip_cap=1, ready_wip_cap=5,
        )

        with self.subTest('active occupant remains admissible'), mock.patch.object(
            WorkflowService, '_wip_roster_config', return_value=config,
        ):
            self.assertIsNone(
                WorkflowService._higher_impact_owned_blocker(
                    occupant, self.EMAIL,
                )
            )
            self.assertTrue(
                WorkflowService.validate_transition(
                    occupant,
                    'in_progress',
                    user=None,
                    screen_data={},
                )
            )

        with self.subTest('ready root is capacity-denied, never focus-elected'), mock.patch.object(
            WorkflowService, '_wip_roster_config', return_value=config,
        ):
            self.assertIsNone(
                WorkflowService._higher_impact_owned_blocker(other, self.EMAIL)
            )
            with self.assertRaises(WorkflowValidationError) as caught:
                WorkflowService.execute_transition(
                    other,
                    'in_progress',
                    user=None,
                    screen_data={},
                )
            self.assertEqual(
                caught.exception.code,
                'WIP_ACTIVE_CAPACITY_DENIED',
            )

    def test_started_review_can_settle_to_changes_requested_after_root_arrives(self):
        """A mid-review root preempts the next pickup, not the current verdict."""
        root = self._item('review settlement root', priority='urgent')
        dependent = self._item('review settlement dependent', priority='urgent')
        self._link_without_auto_block(root, dependent)
        lower = self._item(
            'review already started',
            status='in_review',
            priority='normal',
        )

        transition = WorkflowService.validate_transition(
            lower,
            'in_progress',
            user=None,
            screen_data={},
        )

        self.assertIsNotNone(transition)


    def test_resolved_elsewhere_root_is_not_a_focus_candidate(self):
        """PLAT-8169 arm (a): a blocker root settled as `resolved_elsewhere`
        (PLS-719 — ask answered on the parent) is the completed-receipt token;
        no child work remains to force. Focus demanding it re-deadlocks the
        pair (PLAT-8041 was refused while the stale blocks-link's hold purpose
        was already served). Positive control first: the identical root is
        focus-pinned while runnable, so the later None cannot be fixture drift.
        rfc_* block-releasing slugs stay forceable — those roots still have
        their own remaining stages."""
        root = self._item('escalation blocker root', priority='urgent')
        dependent = self._item('blocked dependent')
        self._link_without_auto_block(root, dependent)
        dependent.status = 'blocked'
        dependent.save(update_fields=['status'])
        lower = self._item('ordinary lower-impact task')

        # False-green guards: the acting item survives every earlier filter.
        self.assertEqual(lower.assignee_email, self.EMAIL)
        self.assertEqual(WorkflowService._active_blockers_for(lower), [])
        self.assertNotIn(
            lower.status,
            WorkflowService._assignment_excluded_statuses(),
        )

        # Positive control: runnable root is focus-pinned.
        self.assertEqual(
            WorkflowService._higher_impact_owned_blocker(lower, self.EMAIL),
            root,
        )

        # Settle the root the PLS-719 way: ask answered on the parent. The
        # PLAT-4215 DB invariant (blocked_item_requires_active_assigned_blocker)
        # requires the release flow to run alongside — a dependent may not stay
        # blocked once its blocker leaves the active band — so release the
        # dependent exactly as the production post-function does.
        with transaction.atomic():
            Item.objects.filter(pk=root.pk).update(status='resolved_elsewhere')
            Item.objects.filter(pk=dependent.pk).update(status='open')
        root.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(root.status, 'resolved_elsewhere')
        self.assertEqual(dependent.status, 'open')
        self.assertTrue(
            WorkflowService._status_releases_blocks(root.status)
        )

        self.assertIsNone(
            WorkflowService._higher_impact_owned_blocker(lower, self.EMAIL)
        )

class BlockerPriorityEscalationTests(TestCase):
    """Operator directive 2026-08-08: a task that blocks active work MUST be
    carried at the highest priority (urgent) so the rebalancer assigns it
    before any lower-priority work — otherwise dependents stay blocked even
    when seats are free."""

    EMAIL = 'blocker-prio-owner@example.com'
    UID = 9111

    def _item(self, title, status='open', priority='normal', **kwargs):
        return Item.objects.create(
            title=title,
            mode='task',
            status=status,
            priority=priority,
            workflow_name='autonomous-dev',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
            **kwargs,
        )

    def _link(self, blocker, dependent):
        link = ItemLink(
            source_item=blocker,
            target_item=dependent,
            link_type='blocks',
            created_by_id=self.UID,
            created_by_email=self.EMAIL,
        )
        link._skip_auto_block = True
        link.save()
        blocker.refresh_from_db()
        dependent.refresh_from_db()
        return link

    def test_link_creation_escalates_blocker_to_urgent(self):
        blocker = self._item('blocker', priority='normal')
        dependent = self._item('dependent', priority='normal')
        self._link(blocker, dependent)
        self.assertEqual(blocker.priority, 'urgent')

    def test_escalation_only_when_descendant_is_active(self):
        blocker = self._item('blocker of resolved work', priority='normal')
        dependent = self._item('resolved dependent', status='done', priority='normal')
        self._link(blocker, dependent)
        self.assertEqual(blocker.priority, 'normal')

    def test_pre_save_backstop_rejects_lowering_blocker_priority(self):
        blocker = self._item('live blocker', priority='normal')
        dependent = self._item('live dependent', priority='normal')
        self._link(blocker, dependent)
        self.assertEqual(blocker.priority, 'urgent')

        blocker.priority = 'high'
        blocker.save(update_fields=['priority'])
        blocker.refresh_from_db()
        # Must remain urgent while it still blocks active work.
        self.assertEqual(blocker.priority, 'urgent')

    def test_priority_returns_after_dependent_resolved(self):
        blocker = self._item('blocker', priority='normal')
        dependent = self._item('dependent', priority='normal')
        self._link(blocker, dependent)
        self.assertEqual(blocker.priority, 'urgent')

        # Resolving the dependent releases the block → priority may drop again.
        dependent.status = 'done'
        dependent.save(update_fields=['status'])
        blocker.priority = 'high'
        blocker.save(update_fields=['priority'])
        blocker.refresh_from_db()
        self.assertEqual(blocker.priority, 'high')

    def test_serializer_rejects_explicit_downgrade_of_active_blocker(self):
        from tasks.serializers import ItemUpdateSerializer
        blocker = self._item('api blocker', priority='urgent')
        dependent = self._item('api dependent', priority='normal')
        self._link(blocker, dependent)

        serializer = ItemUpdateSerializer(
            blocker,
            data={'priority': 'high'},
            partial=True,
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn('priority', serializer.errors)


class WaitingExternalUnfixedReproductionTests(TestCase):
    """PLS-733 red-first probes for the two shared status invariants."""

    EMAIL = 'pls733-owner@example.com'
    UID = 7733

    def _item(self, title, *, status='open', priority='normal'):
        return Item.objects.create(
            title=title,
            mode='task',
            status=status,
            priority=priority,
            workflow_name='autonomous-dev',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
        )

    def _link_without_auto_block(self, blocker, dependent):
        link = ItemLink(
            source_item=blocker,
            target_item=dependent,
            link_type='blocks',
            created_by_id=self.UID,
            created_by_email=self.EMAIL,
        )
        link._skip_auto_block = True
        link.save()

    def _enter_wait(self, item):
        generation = item.external_wait_generation + 1
        authority = ExternalWaitAuthority.objects.create(
            item=item,
            organization_id=item.project.org_id if item.project_id else 1,
            wait_generation=generation,
            branch=ExternalWaitAuthority.BRANCH_HUMAN,
            state=ExternalWaitAuthority.STATE_ENTER_ARMED,
            pre_status=item.status,
        )
        item.external_wait_generation = generation
        item.status = 'waiting_external'
        item._external_wait_authorized = True
        item.save(update_fields=['external_wait_generation', 'status'])
        authority.state = ExternalWaitAuthority.STATE_WAITING
        authority.save(update_fields=['state'])
        return authority

    def test_waiting_external_root_is_not_a_focus_candidate(self):
        root = self._item('human-held escalation root', priority='urgent')
        dependent = self._item('blocked dependent')
        self._link_without_auto_block(root, dependent)
        dependent.status = 'blocked'
        dependent.save(update_fields=['status'])
        lower = self._item('ordinary lower-impact task')

        # False-green guards from ADR-0056 v51: prove the acting item survives
        # every earlier filter before interpreting a None result as success.
        self.assertEqual(lower.assignee_email, self.EMAIL)
        self.assertEqual(WorkflowService._active_blockers_for(lower), [])
        self.assertNotIn(
            lower.status,
            WorkflowService._assignment_excluded_statuses(),
        )

        # Positive control in the same run: the identical root is focus-pinned
        # while runnable, so a later None cannot be fixture construction drift.
        self.assertEqual(
            WorkflowService._higher_impact_owned_blocker(lower, self.EMAIL),
            root,
        )

        root.status = 'waiting_external'
        with self.assertRaisesMessage(ValidationError, 'waiting_external_retired'):
            root.save(update_fields=['status'])
        root.refresh_from_db()
        self.assertNotEqual(root.status, 'waiting_external')
        self.assertEqual(
            WorkflowService._higher_impact_owned_blocker(lower, self.EMAIL),
            root,
        )

    def test_blocked_target_is_not_terminal_for_permission_routing(self):
        for target in ('blocked', 'scheduled', 'waiting_external'):
            with self.subTest(target=target):
                transition = SimpleNamespace(to_status=target)
                self.assertFalse(
                    WorkflowService._transition_target_is_terminal(transition)
                )

    def test_waiting_external_requires_typed_status_authority(self):
        item = self._item('direct mutation must fail closed')
        item.status = 'waiting_external'
        with self.assertRaisesMessage(
            ValidationError, 'waiting_external_retired'
        ):
            item.save(update_fields=['status'])
        item.refresh_from_db()
        self.assertNotEqual(item.status, 'waiting_external')

        # Initial-state writers have no pre-existing carrier generation to
        # authorize.  bulk_create bypasses model hooks, so the insert trigger is
        # the status-level half of the same fail-closed invariant.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Item.objects.bulk_create([Item(
                title='bulk-created bypass attempt',
                mode='task',
                status='waiting_external',
            )])

        # QuerySet/raw writers bypass model clean/save hooks, so the database
        # trigger is a separate load-bearing half of the guarantee.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Item.objects.filter(pk=item.pk).update(status='waiting_external')
        item.refresh_from_db()
        self.assertNotEqual(item.status, 'waiting_external')

    def test_waiting_external_is_nonterminal_block_holding_todo_status(self):
        from tasks.models import Status

        status = Status.objects.get(slug='waiting_external')
        self.assertEqual(status.category, 'todo')
        self.assertTrue(status.is_system)
        self.assertIn('waiting_external', WorkflowService._assignment_excluded_statuses())
        self.assertFalse(WorkflowService._status_is_terminal('waiting_external'))
        self.assertFalse(WorkflowService._status_releases_blocks('waiting_external'))

        ordinary = self._item('ordinary visible queue item')
        waiting = self._item('waiting queue item')
        waiting.status = 'waiting_external'
        with self.assertRaisesMessage(ValidationError, 'waiting_external_retired'):
            waiting.save(update_fields=['status'])
        waiting.refresh_from_db()
        visible_ids = set(
            filter_active_queue_items(
                Item.objects.filter(pk__in=[ordinary.pk, waiting.pk])
            ).values_list('pk', flat=True)
        )
        self.assertIn(ordinary.pk, visible_ids)
        self.assertIn(waiting.pk, visible_ids)

    def test_unassigned_wait_is_absent_from_team_capacity_queue(self):
        project = Project.objects.create(
            name='PLS-733 capacity queue', key='P733Q', org_id=733,
        )
        ordinary = Item.objects.create(
            project=project,
            title='ordinary group queued work',
            mode='task',
            status='open',
            assignment_group='engineering',
        )
        waiting = Item.objects.create(
            project=project,
            title='externally held group queued work',
            mode='task',
            status='open',
            assignment_group='engineering',
        )
        waiting.status = 'waiting_external'
        with self.assertRaisesMessage(ValidationError, 'waiting_external_retired'):
            waiting.save(update_fields=['status'])
        waiting.refresh_from_db()
        queued_ids = {
            item.pk
            for item in WorkflowService._queued_tasks_for_team(
                'engineering', organization_id=733
            )
        }
        self.assertIn(ordinary.pk, queued_ids)
        self.assertIn(waiting.pk, queued_ids)

    def test_waiting_root_is_absent_from_tight_and_loose_capacity_loads(self):
        ordinary = self._item('ordinary owned work')
        waiting = self._item('externally held owned work')
        self.assertEqual(
            WorkflowService._agent_active_load(self.UID, self.EMAIL), 2
        )
        self.assertEqual(
            WorkflowService._agent_workable_load(self.UID, self.EMAIL), 2
        )

        waiting.status = 'waiting_external'
        with self.assertRaisesMessage(ValidationError, 'waiting_external_retired'):
            waiting.save(update_fields=['status'])

        self.assertEqual(
            WorkflowService._agent_active_load(self.UID, self.EMAIL), 2
        )
        self.assertEqual(
            WorkflowService._agent_workable_load(self.UID, self.EMAIL), 2
        )

    def test_absent_metadata_preserves_existing_title_on_wait_entry(self):
        item = self._item('Existing human-facing blocker title')
        item.metadata = {}
        item.save(update_fields=['metadata'])
        item.status = 'waiting_external'
        with self.assertRaisesMessage(ValidationError, 'waiting_external_retired'):
            item.save(update_fields=['status'])
        item.refresh_from_db()
        self.assertEqual(item.metadata, {})
        self.assertEqual(item.title, 'Existing human-facing blocker title')
        self.assertNotEqual(item.status, 'waiting_external')

    def test_waiting_acting_item_is_outside_gate_but_resume_is_fresh_admission(self):
        root = self._item('higher impact blocker', priority='urgent')
        dependent = self._item('blocked by higher impact root')
        self._link_without_auto_block(root, dependent)
        dependent.status = 'blocked'
        dependent.save(update_fields=['status'])
        acting = self._item('lower impact carrier', priority='normal')

        # Same-run positive control and false-green preconditions.
        self.assertEqual(acting.assignee_email, self.EMAIL)
        self.assertEqual(WorkflowService._active_blockers_for(acting), [])
        self.assertNotIn(
            acting.status, WorkflowService._assignment_excluded_statuses()
        )
        self.assertEqual(
            WorkflowService._higher_impact_owned_blocker(acting, self.EMAIL),
            root,
        )

        acting.status = 'waiting_external'
        with self.assertRaisesMessage(ValidationError, 'waiting_external_retired'):
            acting.save(update_fields=['status'])
        acting.refresh_from_db()
        self.assertNotEqual(acting.status, 'waiting_external')
        self.assertEqual(
            WorkflowService._higher_impact_owned_blocker(acting, self.EMAIL),
            root,
        )


class BlockerDatabaseInvariantTests(TransactionTestCase):
    """Deferred PostgreSQL guard covers bulk/raw writers and block release."""

    EMAIL = 'blocker-db-owner@example.com'
    UID = 7720

    def _item(self, title):
        return Item.objects.create(
            title=title,
            mode='task',
            status='open',
            priority='urgent',
            workflow_name='autonomous-dev',
            assignee_id=self.UID,
            assignee_email=self.EMAIL,
        )

    def _linked_block(self, blocker, dependent):
        link = ItemLink(
            source_item=blocker,
            target_item=dependent,
            link_type='blocks',
            created_by_id=self.UID,
            created_by_email=self.EMAIL,
        )
        link._skip_auto_block = True
        link.save()
        dependent.status = 'blocked'
        dependent.save(update_fields=['status'])
        dependent.refresh_from_db()
        self.assertEqual(dependent.status, 'blocked')

    def test_bulk_scheduled_demotion_is_rejected_at_commit(self):
        root = self._item('bulk parked root')
        dependent = self._item('bulk parked dependent')
        self._linked_block(root, dependent)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Item.objects.filter(pk=root.pk).update(status='scheduled')

        root.refresh_from_db()
        self.assertEqual(root.status, 'open')

    def test_manual_block_metadata_cannot_exempt_new_parked_root(self):
        root = self._item('manual block parked root')
        dependent = self._item('manually blocked dependent')
        self._linked_block(root, dependent)
        Item.objects.filter(pk=dependent.pk).update(
            metadata={
                'pre_block_state': {
                    'status': 'open',
                    'had_active_blocker': False,
                },
            },
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Item.objects.filter(pk=root.pk).update(status='scheduled')

        root.refresh_from_db()
        self.assertEqual(root.status, 'open')

    def test_final_terminal_blocker_must_restore_dependent_same_transaction(self):
        root = self._item('terminal root')
        dependent = self._item('terminal dependent')
        self._linked_block(root, dependent)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Item.objects.filter(pk=root.pk).update(status='deferred')

        with transaction.atomic():
            Item.objects.filter(pk=root.pk).update(status='deferred')
            Item.objects.filter(pk=dependent.pk).update(
                status='open',
                workflow_status='open',
            )

        root.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(root.status, 'deferred')
        self.assertEqual(dependent.status, 'open')

    def test_resolution_must_restore_final_dependent_same_transaction(self):
        root = self._item('resolved root')
        dependent = self._item('resolved dependent')
        self._linked_block(root, dependent)
        resolution = Resolution.objects.create(
            name='Blocker test fixed',
            slug='blocker-test-fixed',
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Item.objects.filter(pk=root.pk).update(
                    resolution_id=resolution.pk,
                )

        with transaction.atomic():
            Item.objects.filter(pk=root.pk).update(
                resolution_id=resolution.pk,
            )
            Item.objects.filter(pk=dependent.pk).update(
                status='open',
                workflow_status='open',
            )

        root.refresh_from_db()
        dependent.refresh_from_db()
        self.assertEqual(root.resolution_id, resolution.pk)
        self.assertEqual(dependent.status, 'open')

    def test_one_terminal_blocker_can_leave_other_live_block_holding(self):
        first = self._item('first terminal blocker')
        second = self._item('second live blocker')
        dependent = self._item('multi blocker dependent')
        self._linked_block(first, dependent)
        second_link = ItemLink(
            source_item=second,
            target_item=dependent,
            link_type='blocks',
            created_by_id=self.UID,
            created_by_email=self.EMAIL,
        )
        second_link._skip_auto_block = True
        second_link.save()

        with transaction.atomic():
            Item.objects.filter(pk=first.pk).update(status='deferred')

        dependent.refresh_from_db()
        self.assertEqual(dependent.status, 'blocked')
