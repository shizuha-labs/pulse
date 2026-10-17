"""PLS-756: Cancel/Discard reason-category is a server-owned, discoverable
contract.

Regression: pulse_get_available_transitions advertised Cancel as requiring only
`comment`; the runtime guard's required `reason_category` (duplicate|invalid|
obsolete|test-artifact) was only discoverable from the frontend's hardcoded
dropdown. This test pins:
1. Discovery (WorkflowService.get_available_transitions) reports the
   reason_category field with the exact enum for Cancel/Discard transitions,
   even when stored screen_fields predate the contract (backstop).
2. Execute honors a structured screen_data.reason_category and rejects unknown
   values with an actionable message.
3. The comment-keyword path (backward compat) still cancels without a
   structured field — the generic required-screen gate must not hard-require it.
"""

from django.test import TestCase

from tasks.models import (
    Comment, Item, Project, ProjectWorkflow, Status, Workflow, WorkflowScheme,
    WorkflowStatus, WorkflowTransition,
)
from tasks.services.workflow_service import WorkflowService, WorkflowValidationError


class CancelReasonCategoryContractTests(TestCase):

    def setUp(self):
        self.open = Status.objects.create(name='PLS756 Open', slug='pls756_open', category='todo')
        self.done = Status.objects.create(name='PLS756 Done', slug='pls756_done', category='done')
        cancelled = Status.objects.filter(slug='cancelled').first()
        if cancelled is None:
            cancelled = Status.objects.create(
                name='PLS756 Cancelled', slug='cancelled', category='cancelled')
        self.wf = Workflow.objects.create(name='PLS756 WF', slug='pls756-wf', initial_status=self.open)
        WorkflowStatus.objects.create(workflow=self.wf, status=self.open, position=0)
        WorkflowStatus.objects.create(workflow=self.wf, status=self.done, position=1)
        WorkflowStatus.objects.create(workflow=self.wf, status=cancelled, position=2)
        import uuid
        self.project = Project.objects.create(
            name='PLS756 Project', key='PLS756' + uuid.uuid4().hex[:4].upper(),
            org_id=1, access_level='open')
        # Scheme-backed so available-transitions resolves the real Workflow
        # (the scheme branch returns full transition metadata incl.
        # screen_fields; the legacy branch would not).
        scheme = WorkflowScheme.objects.create(
            name='PLS756 Scheme', slug='pls756-scheme', default_workflow=self.wf)
        ProjectWorkflow.objects.create(
            project=self.project, workflow_scheme=scheme, statuses=[])
        # A Cancel transition seeded BEFORE the PLS-756 contract existed: only
        # the comment screen field is stored.
        self.cancel = WorkflowTransition.objects.create(
            workflow=self.wf,
            name='Cancel / Discard',
            from_status='pls756_open',
            to_status='cancelled',
            button_style='danger',
            screen_fields=[{'field': 'comment', 'label': 'Why?', 'required': True}],
        )
        self.item = Item.objects.create(
            mode='task', title='pls756 item', project=self.project, workflow_name='pls756-wf')

    def test_discovery_exposes_reason_category_with_enum(self):
        transitions = WorkflowService.get_available_transitions(
            self.project, 'pls756_open', item=self.item)
        cancel = next(t for t in transitions if t['to_status'] == 'cancelled')
        fields = {f.get('field'): f for f in cancel['screen_fields']}
        self.assertIn('reason_category', fields)
        rc = fields['reason_category']
        self.assertTrue(rc['required'])
        self.assertEqual(rc['enum'], ['duplicate', 'invalid', 'obsolete', 'test-artifact'])
        self.assertIn('comment', fields)  # preserved alongside the category

    def test_direct_helper_is_idempotent(self):
        fields = WorkflowService._ensure_cancel_reason_category_field(
            [{'field': 'comment', 'required': True}])
        again = WorkflowService._ensure_cancel_reason_category_field(fields)
        self.assertEqual(len(again), len(fields))
        self.assertEqual(
            [f['field'] for f in again],
            ['comment', 'reason_category'],
        )

    def test_execute_accepts_structured_reason_category(self):
        # No fix evidence, so cancel passes the guard with the structured field.
        WorkflowService._validate_cancel_preconditions(
            self.item, {'reason_category': 'invalid', 'comment': 'dup of PLS-1'})

    def test_execute_rejects_unknown_reason_category_with_actionable_message(self):
        self.item.metadata = {}
        self.item.save()
        with self.assertRaises(WorkflowValidationError) as ctx:
            WorkflowService._validate_cancel_preconditions(
                self.item, {'reason_category': 'bogus'})
        message = str(ctx.exception)
        self.assertIn('duplicate', message)
        self.assertIn('invalid', message)
        self.assertIn('test-artifact', message)
        self.assertIn('obsolete', message)

    def test_comment_keyword_path_still_cancels_without_structured_field(self):
        # Backward compatibility: a cancel comment that names the category must
        # pass even when no structured reason_category is provided.
        Comment.objects.create(item=self.item, author_id=1, author_email='t@example.com',
                               content='invalid — closing as not a bug')
        WorkflowService._validate_cancel_preconditions(
            self.item, {'reason_category': '', 'comment': 'invalid — closing as not a bug'})

    def test_required_screen_gate_does_not_double_require_validator_owned_field(self):
        # The generic required-screen gate must treat reason_category as
        # validator-owned: a caller supplying the category only inside the
        # comment keyword (no structured screen value) is not rejected for
        # missing screen fields — _validate_cancel_preconditions is the single
        # enforcement point and accepts structured OR comment-keyword.
        transition = WorkflowService.validate_transition(
            self.item, 'cancelled',
            screen_data={'comment': 'duplicate — superseded by PLS-1'},
            transition_id=str(self.cancel.id),
        )
        self.assertEqual(transition.to_status, 'cancelled')

    def test_validate_transition_accepts_structured_reason_category(self):
        transition = WorkflowService.validate_transition(
            self.item, 'cancelled',
            screen_data={'comment': 'dup ticket', 'reason_category': 'invalid'},
            transition_id=str(self.cancel.id),
        )
        self.assertEqual(transition.to_status, 'cancelled')


class CancelCommentKeywordBackCompatTests(TestCase):
    """PLS-756 must not regress keyword-only cancels that pre-date the field."""

    def test_keyword_in_comment_only(self):
        item = Item.objects.create(mode='task', title='kw only', workflow_name='simple', status='open')
        Comment.objects.create(item=item, author_id=1, author_email='t@example.com',
                               content='duplicate of another ticket')
        WorkflowService._validate_cancel_preconditions(item, {'comment': 'duplicate of another ticket'})
