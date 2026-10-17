"""Focused-QA dispositions preserve history and a non-circular canonical target."""
import threading
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connections, transaction
from django.test import TestCase, TransactionTestCase

from tasks.models import FocusedQADispositionReceipt, Item, ItemActivity, ItemLink, Project
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError
from tasks.tests.test_pls1185_disposition_wiring import _transition
from tasks.tests.test_pls824_focused_qa_disposition_core import _make_generation, _make_origin
from tasks.views import ItemViewSet


class FocusedQAIntegrityTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(key='QAI', name='QA integrity', org_id=1)
        self.origin = _make_origin(project=self.project)
        self.actor = SimpleNamespace(
            id=22, email='reviewer@shizuha.com', is_staff=False, is_superuser=False,
        )
        self.next_number = 1

    def qa_task(self, *, acceptance_mode='code', status='in_review'):
        task = Item.objects.create(
            project=self.project, title='Focused QA verification',
            item_key=f'QAI-{self.next_number}', workflow_name='focused-qa',
            status=status, acceptance_mode=acceptance_mode,
            source='focused-qa-automation', source_id=str(self.origin.pk),
        )
        self.next_number += 1
        _make_generation(
            project=self.project, origin=self.origin, qa_task=task,
            generation_key=f'gen:{task.pk}',
        )
        return task

    def subsume(self, task, target):
        return WorkflowService.execute_transition(
            task, 'done', user=self.actor,
            transition_id=str(_transition('Close Duplicate / Subsumed').pk),
            screen_data={'target_task': target.item_key, 'comment': 'Duplicate evidence carrier'},
        )

    def link(self, source, target, kind='duplicates'):
        return ItemLink.objects.create(
            source_item=source, target_item=target, link_type=kind,
            created_by_id=self.actor.id, created_by_email=self.actor.email,
        )

    def test_reciprocal_subsumption_rolls_back_second_transition_and_receipt(self):
        canonical, echo = self.qa_task(), self.qa_task()
        self.subsume(echo, canonical)
        first_receipt = FocusedQADispositionReceipt.objects.get(focused_qa_task=echo)
        with self.assertRaisesMessage(WorkflowValidationError, 'circular subsumption'):
            self.subsume(canonical, echo)
        canonical.refresh_from_db()
        echo.refresh_from_db()
        self.assertEqual(canonical.status, 'in_review')
        self.assertEqual(echo.status, 'done')
        self.assertEqual(canonical.activities.filter(action='status_changed').count(), 0)
        self.assertFalse(canonical.focused_qa_receipts.exists())
        self.assertEqual(echo.focused_qa_receipts.get().pk, first_receipt.pk)
        self.assertEqual(ItemLink.objects.filter(link_type='duplicates').count(), 1)

    def test_self_subsumption_is_rejected_without_side_effects(self):
        task = self.qa_task()
        with self.assertRaisesMessage(WorkflowValidationError, 'circular subsumption'):
            self.subsume(task, task)
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_review')
        self.assertFalse(task.focused_qa_receipts.exists())
        self.assertFalse(ItemLink.objects.exists())

    def test_transitive_cycle_including_inverse_link_is_rejected(self):
        canonical, middle, echo = self.qa_task(), self.qa_task(), self.qa_task()
        self.link(canonical, middle, 'is_duplicated_by')
        self.subsume(echo, middle)
        with self.assertRaisesMessage(WorkflowValidationError, 'circular subsumption'):
            self.subsume(canonical, echo)
        canonical.refresh_from_db()
        self.assertEqual(canonical.status, 'in_review')
        self.assertFalse(canonical.focused_qa_receipts.exists())

    def test_converging_chains_and_same_direction_inverse_link_are_valid(self):
        canonical, middle, echo = self.qa_task(), self.qa_task(), self.qa_task()
        self.link(middle, canonical)
        self.link(canonical, middle, 'is_duplicated_by')
        self.link(echo, canonical)
        self.subsume(echo, middle)
        self.assertEqual(echo.focused_qa_receipts.get().disposition, 'subsumed')

    def test_existing_legacy_cycle_cannot_subsume_another_task(self):
        first, second, echo = self.qa_task(), self.qa_task(), self.qa_task()
        ItemLink.objects.bulk_create([
            ItemLink(
                source_item=first, target_item=second, link_type='duplicates',
                created_by_id=self.actor.id, created_by_email=self.actor.email,
            ),
            ItemLink(
                source_item=second, target_item=first, link_type='duplicates',
                created_by_id=self.actor.id, created_by_email=self.actor.email,
            ),
        ])
        with self.assertRaisesMessage(WorkflowValidationError, 'circular subsumption'):
            self.subsume(echo, first)
        echo.refresh_from_db()
        self.assertEqual(echo.status, 'in_review')
        self.assertFalse(echo.focused_qa_receipts.exists())
        self.assertEqual(ItemLink.objects.count(), 2)

    def test_diamond_converging_on_one_canonical_task_is_not_a_cycle(self):
        canonical, left, right, middle, echo = [self.qa_task() for unused in range(5)]
        self.link(left, canonical)
        self.link(right, canonical)
        self.link(middle, left)
        self.link(middle, right)
        self.subsume(echo, middle)
        self.assertEqual(echo.focused_qa_receipts.get().disposition, 'subsumed')

    def test_link_retarget_cannot_form_cycle(self):
        canonical, middle, echo = self.qa_task(), self.qa_task(), self.qa_task()
        edge = self.link(middle, canonical)
        self.link(echo, middle)
        edge.target_item = echo
        with self.assertRaisesMessage(ValidationError, 'circular subsumption'):
            edge.save(update_fields=['target_item'])
        edge.refresh_from_db()
        self.assertEqual(edge.target_item_id, canonical.pk)

    def test_unrelated_relation_cycles_are_unchanged(self):
        first, second = self.qa_task(), self.qa_task()
        self.link(first, second, 'relates_to')
        self.link(second, first, 'relates_to')
        self.assertEqual(ItemLink.objects.count(), 2)

    def test_mark_duplicate_api_returns_caller_error_without_status_write(self):
        canonical = Item.objects.create(
            project=self.project, title='Canonical', item_key='QAI-9001', status='in_progress',
        )
        echo = Item.objects.create(
            project=self.project, title='Echo', item_key='QAI-9002', status='in_progress',
        )
        self.link(echo, canonical)
        request = SimpleNamespace(
            data={'target_item_key': echo.item_key}, user=self.actor,
        )
        with patch.object(ItemViewSet, 'get_object', return_value=canonical):
            response = ItemViewSet().mark_duplicate(request, pk=canonical.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn('circular subsumption', response.data['error'])
        canonical.refresh_from_db()
        self.assertEqual(canonical.status, 'in_progress')
        self.assertNotIn('duplicate_of', canonical.metadata or {})

    def test_direct_duplicate_api_cannot_bypass_focused_qa_receipt(self):
        canonical, echo = self.qa_task(), self.qa_task()
        request = SimpleNamespace(
            data={'target_item_key': canonical.item_key}, user=self.actor,
        )
        with patch.object(ItemViewSet, 'get_object', return_value=echo):
            response = ItemViewSet().mark_duplicate(request, pk=echo.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn('typed receipt', response.data['error'])
        echo.refresh_from_db()
        self.assertEqual(echo.status, 'in_review')
        self.assertFalse(echo.focused_qa_receipts.exists())
        self.assertFalse(ItemLink.objects.exists())

    def test_blocked_done_return_then_cancel_cannot_erase_review_history(self):
        task = self.qa_task(acceptance_mode='live')
        with self.assertRaisesMessage(WorkflowValidationError, 'missing_merged_head_sha'):
            WorkflowService.execute_transition(
                task, 'done', user=self.actor,
                transition_id=str(_transition('Confirm Product Failure').pk),
                screen_data={'qa_failure_evidence': 'Test evidence'},
            )
        WorkflowService.execute_transition(
            task, 'in_progress', user=self.actor,
            transition_id=str(_transition('Return Evidence for Correction').pk),
            screen_data={'comment': 'Test'},
        )
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        before_receipts = list(task.focused_qa_receipts.values_list('pk', flat=True))
        with self.assertRaises(WorkflowValidationError) as caught:
            WorkflowService.execute_transition(
                task, 'cancelled', user=self.actor,
                screen_data={'comment': 'Test', 'reason_category': 'invalid'},
            )
        self.assertEqual(caught.exception.code, 'FOCUSED_QA_PROTECTED_REVIEW_HISTORY')
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_progress')
        self.assertNotIn('cancel_audit', task.metadata)
        self.assertEqual(list(task.focused_qa_receipts.values_list('pk', flat=True)), before_receipts)

    def test_pre_review_invalid_artifact_can_still_cancel(self):
        task = self.qa_task(status='todo')
        WorkflowService.execute_transition(
            task, 'cancelled', user=self.actor,
            screen_data={'comment': 'Test', 'reason_category': 'test-artifact'},
        )
        task.refresh_from_db()
        self.assertEqual(task.status, 'cancelled')
        self.assertFalse(task.focused_qa_receipts.exists())

    def test_legacy_focused_qa_review_history_remains_protected(self):
        task = self.qa_task()
        task.status = 'in_progress'
        task.workflow_status = 'in_progress'
        task.metadata = {'previous_status': 'in_review'}
        task.save(update_fields=['status', 'workflow_status', 'metadata'])
        task.activities.all().delete()
        with self.assertRaisesMessage(WorkflowValidationError, 'protected review history'):
            WorkflowService._validate_cancel_preconditions(
                task, {'reason_category': 'invalid', 'comment': 'Test'},
            )

    def test_generic_historical_review_without_delivery_does_not_trap_cancel(self):
        task = Item.objects.create(
            project=self.project, title='Obsolete artifact', status='in_progress',
            workflow_name='reviewed-task',
        )
        ItemActivity.objects.create(
            item=task, action='status_changed', field_name='Status',
            old_value='in_review', new_value='in_progress',
            user_id=self.actor.id, user_email=self.actor.email,
        )
        WorkflowService._validate_cancel_preconditions(
            task, {'reason_category': 'obsolete', 'comment': 'Obsolete artifact'},
        )

    def test_typed_waiver_still_allows_honest_subsumption(self):
        canonical, echo = self.qa_task(), self.qa_task(acceptance_mode='live')
        with patch.object(WorkflowService, '_is_team_member', return_value=True):
            WorkflowService.issue_deploy_verify_waiver(
                echo, actor=self.actor, reason='Echo carrier; product defect remains open',
                event_id=f'waiver:{echo.pk}',
                evidence={
                    'kind': 'inline_package', 'actor': self.actor.email,
                    'originating_key': echo.item_key,
                    'reason': 'Echo carrier; product defect remains open',
                    'target_head': 'a' * 40,
                    'verdict_refs': [canonical.item_key],
                    'scope_conditions': ['Only the duplicate verification artifact'],
                },
            )
        self.subsume(echo, canonical)
        echo.refresh_from_db()
        self.assertEqual(echo.status, 'done')
        self.assertEqual(echo.focused_qa_receipts.get().disposition, 'subsumed')
        self.assertEqual(echo.resolution.slug, 'duplicate')


class DuplicateGraphConcurrencyTests(TransactionTestCase):
    def test_concurrent_reciprocal_links_serialize_before_graph_validation(self):
        project = Project.objects.create(key='QAC', name='Concurrent QA', org_id=1)
        first = Item.objects.create(project=project, title='First')
        second = Item.objects.create(project=project, title='Second')
        first_checked = threading.Event()
        second_checked = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        results = []
        original_validate = ItemLink._validate_duplicate_graph

        def validate(link, using=None):
            if link.source_item_id == second.pk:
                second_checked.set()
            original_validate(link, using=using)
            if link.source_item_id == first.pk:
                first_checked.set()
                if not release_first.wait(10):
                    raise AssertionError('First graph writer was not released')

        def write(source_id, target_id):
            try:
                if source_id == second.pk:
                    second_started.set()
                with transaction.atomic():
                    with connections['default'].cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '5s'")
                    ItemLink.objects.create(
                        source_item_id=source_id, target_item_id=target_id,
                        link_type='duplicates', created_by_id=0,
                        created_by_email='qa@shizuha.com',
                    )
                results.append('accepted')
            except ValidationError:
                results.append('rejected')
            except Exception as exc:
                results.append(repr(exc))
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=write, args=(first.pk, second.pk)),
            threading.Thread(target=write, args=(second.pk, first.pk)),
        ]
        with patch.object(ItemLink, '_validate_duplicate_graph', validate):
            try:
                threads[0].start()
                self.assertTrue(first_checked.wait(10))
                threads[1].start()
                self.assertTrue(second_started.wait(10))
                self.assertFalse(second_checked.wait(0.2))
            finally:
                release_first.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertCountEqual(results, ['accepted', 'rejected'])
        self.assertEqual(ItemLink.objects.count(), 1)
