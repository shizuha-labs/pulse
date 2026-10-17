"""PLS-1185 — the focused-QA disposition must fire from transition execution.

The disposition service was complete but never wired: the seeded FAIL/SUBSUMED
transitions carried only set_resolution + send_notification, and no post-function
executor type dispatched ``commit_disposition``. Live canaries PLAT-7637/7639/7640
caught the silent no-ops. These tests drive the REAL seeded workflow transitions
through ``WorkflowService.execute_transition`` (production order) and assert the
promised side effects commit atomically — and that a degenerate task (no
generation) fails the transition loudly instead of silently no-oping.
"""
from django.test import TestCase

from tasks.models import (
    FocusedQADispositionReceipt,
    Item,
    ItemLink,
    WorkflowTransition,
)
from tasks.services.focused_qa_disposition import find_or_create_generation
from tasks.services.workflow_service import WorkflowService


from tasks.tests.test_pls824_focused_qa_disposition_core import (  # noqa: E402
    _make_generation,
    _make_origin,
    _make_project,
    _make_qa_task,
)


def _focused_qa_task(project, origin):
    task = _make_qa_task(project, origin)
    task.workflow_name = 'focused-qa'
    task.save(update_fields=['workflow_name'])
    return task


def _transition(name):
    return WorkflowTransition.objects.get(workflow__slug='focused-qa', name=name)


class DispositionWiringTests(TestCase):
    def setUp(self):
        self.project = _make_project()
        self.origin = _make_origin(project=self.project)

    def test_fail_transition_commits_receipt_bug_and_links(self):
        task = _focused_qa_task(self.project, self.origin)
        _make_generation(project=self.project, origin=self.origin, qa_task=task)

        WorkflowService.execute_transition(
            task,
            'done',
            transition_id=str(_transition('Confirm Product Failure').pk),
            screen_data={'qa_failure_evidence': 'SYMPTOM-X observed live'},
        )

        task.refresh_from_db()
        self.assertEqual(task.status, 'done')
        receipt = FocusedQADispositionReceipt.objects.get(focused_qa_task=task)
        self.assertEqual(receipt.disposition, 'fail')
        self.assertIsNotNone(receipt.product_bug)
        bug = receipt.product_bug
        self.assertNotEqual(bug.pk, task.pk)
        # The FAIL link contract (HLD §4.3): qa task + origin relate to the bug.
        self.assertTrue(
            ItemLink.objects.filter(source_item=task, target_item=bug, link_type='relates_to').exists()
        )
        self.assertTrue(
            ItemLink.objects.filter(source_item=self.origin, target_item=bug, link_type='relates_to').exists()
        )

    def test_subsumed_transition_writes_duplicate_link(self):
        task = _focused_qa_task(self.project, self.origin)
        _make_generation(project=self.project, origin=self.origin, qa_task=task)
        target = Item.objects.create(
            project=self.project, title='Canonical task', status='in_progress',
            workflow_name='autonomous-bug',
            # Item.save() does not auto-generate item_key (the API layer
            # does); the SUBSUMED validator reads screen_data.target_task,
            # so the test must supply a real project-scoped key.
            item_key=f'{self.project.key}-9001',
        )

        WorkflowService.execute_transition(
            task,
            'done',
            transition_id=str(_transition('Close Duplicate / Subsumed').pk),
            screen_data={'target_task': target.item_key, 'comment': 'subsumed by canonical task'},
        )

        task.refresh_from_db()
        self.assertEqual(task.status, 'done')
        self.assertEqual(task.resolution.slug if task.resolution else '', 'duplicate')
        receipt = FocusedQADispositionReceipt.objects.get(focused_qa_task=task)
        self.assertEqual(receipt.disposition, 'subsumed')
        self.assertTrue(
            ItemLink.objects.filter(
                source_item=task, target_item=target, link_type='duplicates'
            ).exists()
        )

    def test_subsumed_without_target_fails_loud_and_rolls_back(self):
        task = _focused_qa_task(self.project, self.origin)
        _make_generation(project=self.project, origin=self.origin, qa_task=task)

        with self.assertRaises(Exception):
            WorkflowService.execute_transition(
                task,
                'done',
                transition_id=str(_transition('Close Duplicate / Subsumed').pk),
                screen_data={'comment': 'no target supplied'},
            )
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_review')
        self.assertFalse(FocusedQADispositionReceipt.objects.filter(focused_qa_task=task).exists())

    def test_fail_without_generation_fails_loud(self):
        task = _focused_qa_task(self.project, self.origin)
        # No generation bound — the disposition cannot commit; the transition
        # must fail loudly (the silent-no-op class is the bug being fixed).

        with self.assertRaises(Exception):
            WorkflowService.execute_transition(
                task,
                'done',
                transition_id=str(_transition('Confirm Product Failure').pk),
                screen_data={'qa_failure_evidence': 'SYMPTOM-X observed live'},
            )
        task.refresh_from_db()
        self.assertEqual(task.status, 'in_review')
        self.assertFalse(FocusedQADispositionReceipt.objects.filter(focused_qa_task=task).exists())
