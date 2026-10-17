"""HIVE-2176 regression: field_required validator must read screen_data.

The focused-qa "Pass QA Evidence" transition declares a ``field_required``
validator on the screen field ``comment``. Before this fix, ``_run_validators``
only read ``getattr(item, field, None)`` — an Item has no ``comment`` attribute,
so the validator always raised and every focused-qa closure was blocked.

The fix makes ``field_required`` read the transition payload (``screen_data``)
first, then fall back to the item attribute — matching ``_check_required_fields``
semantics. These tests pin that contract:
- a screen field (``comment``) passes when supplied in ``screen_data``;
- it fails when ``screen_data`` is missing the field and the item lacks it;
- item-field behavior (``title``) is preserved (reads from the item);
- a blank/whitespace screen value still fails (fail-closed).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import Item, Project, Status, Workflow, WorkflowStatus, WorkflowTransition
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


class FieldRequiredScreenDataTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='hive2176',
            email='hive2176@example.com',
            password='testpass123',
            is_staff=True,
        )

        self.open_status, _ = Status.objects.get_or_create(
            slug='open',
            defaults={'name': 'Open', 'category': 'todo', 'position': 1},
        )
        self.done_status, _ = Status.objects.get_or_create(
            slug='done',
            defaults={'name': 'Done', 'category': 'done', 'position': 2},
        )
        self.workflow = Workflow.objects.create(
            name='Focused QA HIVE2176',
            slug='focused-qa-hive2176',
            initial_status=self.open_status,
        )
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.open_status, position=1)
        WorkflowStatus.objects.create(workflow=self.workflow, status=self.done_status, position=2)

        self.project = Project.objects.create(name='HIVE2176 Project', key='H2176', org_id=1)
        self.item = Item.objects.create(
            mode='task',
            title='QA evidence task',
            project=self.project,
            item_key='H2176-1',
            status='open',
            workflow_name=self.workflow.name,
            created_by_id=self.user.id,
        )

    def _transition(self, field):
        return WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Pass QA Evidence',
            from_status='open',
            to_status='done',
            validators=[{'type': 'field_required', 'field': field}],
            post_functions=[],
            screen_fields=[],
            allowed_roles=[],
        )

    def test_screen_field_comment_passes_when_supplied_in_screen_data(self):
        """HIVE-2176: comment supplied in screen_data satisfies field_required."""
        transition = self._transition('comment')
        # No exception expected.
        WorkflowService._run_validators(
            transition, self.item, self.user,
            screen_data={'comment': 'Evidence assessment: PASS — repro verified live.'},
        )

    def test_screen_field_comment_fails_when_missing(self):
        """HIVE-2176: missing screen_data comment still fails (fail-closed)."""
        transition = self._transition('comment')
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._run_validators(transition, self.item, self.user)
        self.assertIn('comment', str(ctx.exception))

    def test_screen_field_blank_value_fails(self):
        """HIVE-2176: blank/whitespace screen value is rejected."""
        transition = self._transition('comment')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(
                transition, self.item, self.user,
                screen_data={'comment': '   '},
            )

    def test_item_field_title_still_reads_from_item(self):
        """Item-field behavior preserved: title reads from the item, not screen_data."""
        transition = self._transition('title')
        # Item has a title; no screen_data needed.
        WorkflowService._run_validators(transition, self.item, self.user)

    def test_item_field_title_fails_when_item_field_empty(self):
        """Item-field behavior preserved: empty item title still fails."""
        transition = self._transition('title')
        self.item.title = ''
        self.item.save(update_fields=['title'])
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(transition, self.item, self.user)

    def test_screen_data_wins_over_item_attribute(self):
        """screen_data takes precedence when both item and payload provide a value."""
        transition = self._transition('title')
        # Item title is set, but a blank screen value must win (fail-closed).
        with self.assertRaises(WorkflowValidationError):
            WorkflowService._run_validators(
                transition, self.item, self.user,
                screen_data={'title': '  '},
            )
