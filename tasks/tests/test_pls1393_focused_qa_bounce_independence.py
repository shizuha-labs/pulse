"""PLS-1393: focused-QA no-code verification legs never bounce to their author.

Live defect (CON-495, 2026-10-01): a completed no-code QA evidence pack
submitted for review was Request-Changes-bounced back to its verification
author as fresh work, cycled Review→author→QA re-assertion, and never reached
an independent disposition seat.

Contract under test:
1. A focused-QA verification leg (identity-based, NO linked PR) departing a
   review stage via Request Changes must NOT return to the verification
   author's concrete seat. The durable ladder routes it to the focused-QA lane
   queue (focused_qa_assignment_group, group-queue seat) — an independent
   disposition seat. Genuine evidence corrections own the dedicated
   'Return Evidence for Correction' disposition.
2. The exemption is no-code-SHAPED, not leg-shaped: the same leg WITH a linked
   PR still returns to its recorded implementer.
3. Focused-QA legs are stamped with the `no-pr` classification AT GENERATION
   (structured source/risk labels preserved), so downstream no-pr-aware gates
   see it without a reviewer's manual correction.
"""
import itertools
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase

from tasks.models import (
    Item,
    Project,
    Status,
    TeamRoster,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)
from tasks.services.focused_qa_disposition import _bind_or_create_task
from tasks.services.workflow_service import WorkflowService

User = get_user_model()
_key_counter = itertools.count(1)


class FocusedQaBounceIndependenceTests(TransactionTestCase):
    """TransactionTestCase so SET CONSTRAINTS ALL IMMEDIATE flushes the
    deferred PLS-467 marker inside the test, not only at TestCase teardown.
    """
    def setUp(self):
        self.actor = User.objects.create_user(
            username='pls1393-actor', email='pls1393-actor@shizuha.com',
            password='test', is_staff=True,
        )
        self.author = User.objects.create_user(
            username='pls1393-qa-author', email='pls1393-qa-author@shizuha.com',
            password='test',
        )
        self.reviewer = User.objects.create_user(
            username='pls1393-reviewer', email='pls1393-reviewer@shizuha.com',
            password='test', is_staff=True,
        )
        self.project = Project.objects.create(
            name='PLS1393 focused-QA routing project', key='P39', org_id=1,
        )
        for team_name, emails in (
            ('qa-automation', [self.author.email]),
            ('review', [self.reviewer.email]),
        ):
            TeamRoster.objects.update_or_create(
                organization_id=self.project.org_id,
                team_name=team_name,
                defaults={
                    'organization_slug': 'pls1393-test',
                    'tiers': [emails],
                    'is_active': True,
                },
            )
        statuses = {}
        for slug, name in (('in_progress', 'In Progress'), ('in_review', 'In Review')):
            statuses[slug], _ = Status.objects.get_or_create(
                slug=slug, defaults={'name': name, 'category': 'in_progress'},
            )
        self.workflow = Workflow.objects.create(
            name='PLS1393 Reviewed Task', slug='pls1393-reviewed-task',
            initial_status=statuses['in_progress'], is_active=True,
        )
        for position, slug in enumerate(('in_progress', 'in_review')):
            WorkflowStatus.objects.create(
                workflow=self.workflow, status=statuses[slug], position=position,
            )
        self.request_changes = WorkflowTransition.objects.create(
            workflow=self.workflow,
            name='Request Changes',
            from_status='in_review',
            to_status='in_progress',
            position=0,
            post_functions=[{'type': 'assign_implementer'}],
        )

    def _leg(self, *, with_pr):
        origin = Item.objects.create(
            item_key=f'P39-ORIGIN-{next(_key_counter)}',
            title='origin task awaiting independent disposition',
            project=self.project,
            status='in_progress',
            workflow_name=self.workflow.slug,
            created_by_id=self.author.id,
            created_by_email=self.author.email,
        )
        metadata = {
            'focused_qa_origin_item_id': str(origin.pk),
            'focused_qa_origin_item_key': origin.item_key,
            'focused_qa_assignment_group': 'qa-automation',
            'focused_qa_routing_mode': 'qa-roster',
            'implementation_return_context': {
                'version': 1,
                'implementer': {'id': self.author.id, 'email': self.author.email},
                'implementation_team': 'qa-automation',
                'review_generation': 3,
            },
        }
        if with_pr:
            metadata['pull_requests'] = [{
                'url': 'https://origin.shizuha.com/shizuha-labs/hive/pulls/178',
                'repo': 'shizuha-labs/hive', 'number': 178,
                'status': 'open', 'head_sha': 'a' * 40,
            }]
        return Item.objects.create(
            item_key=f'P39-LEG-{next(_key_counter)}',
            title='Focused QA verification: P39-ORIGIN',
            project=self.project,
            mode='task',
            source='focused-qa-automation',
            status='in_review',
            workflow_name=self.workflow.slug,
            workflow_status='in_review',
            assignment_group='review',
            assignee_id=self.reviewer.id,
            assignee_email=self.reviewer.email,
            created_by_id=self.author.id,
            created_by_email=self.author.email,
            labels={'source': 'focused-qa', 'risk_label': 'requires-live-qa'},
            tags='focused-qa',
            metadata=metadata,
        )

    def _bounce(self, leg):
        with mock.patch.object(
            WorkflowService, '_is_agent_available_for_assignment', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ), mock.patch.object(
            WorkflowService, '_resolve_team_assignee', return_value=(None, None),
        ), mock.patch.object(
            WorkflowService, '_resolve_team_lead_email', return_value=None,
        ):
            with transaction.atomic():
                WorkflowService.execute_transition(
                    item=leg,
                    new_status='in_progress',
                    user=self.reviewer,
                    screen_data={},
                    transition_id=str(self.request_changes.id),
                )
                with connection.cursor() as cursor:
                    cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

    def test_no_code_leg_bounces_to_focused_qa_queue_never_author(self):
        leg = self._leg(with_pr=False)
        self._bounce(leg)
        leg.refresh_from_db()
        self.assertEqual(leg.status, 'in_progress')
        # Independent disposition seat: the focused-QA lane GROUP QUEUE.
        self.assertEqual(leg.assignment_group, 'qa-automation')
        self.assertIn(leg.assignee_id, (None, 0))
        self.assertFalse((leg.assignee_email or '').strip())
        self.assertNotEqual(
            (leg.assignee_email or '').strip().lower(), self.author.email,
        )

    def test_code_shaped_leg_still_returns_to_recorded_implementer(self):
        leg = self._leg(with_pr=True)
        self._bounce(leg)
        leg.refresh_from_db()
        self.assertEqual(leg.status, 'in_progress')
        self.assertEqual(
            (leg.assignee_email or '').strip().lower(), self.author.email,
        )

    def test_generation_bind_stamps_no_pr_classification(self):
        origin = Item.objects.create(
            item_key=f'P39-ORIGIN-BIND-{next(_key_counter)}',
            title='origin for bind stamping',
            project=self.project,
            status='in_progress',
            workflow_name=self.workflow.slug,
            created_by_id=self.author.id,
            created_by_email=self.author.email,
        )
        leg = _bind_or_create_task(
            originating_task=origin,
            organization='1',
            environment='live',
            observed_build='',
            metadata={},
        )
        # Structured source/risk labels preserved; no-pr classification appended.
        self.assertEqual(leg.labels.get('source'), 'focused-qa')
        self.assertEqual(leg.labels.get('risk_label'), 'requires-live-qa')
        self.assertTrue(leg.labels.get('no-pr'))
        self.assertIn('no-pr', str(leg.tags or '').split(','))
        # Idempotent re-bind returns the SAME row (existing-generation branch).
        again = _bind_or_create_task(
            originating_task=origin,
            organization='1',
            environment='live',
            observed_build='',
            metadata={},
        )
        self.assertEqual(again.pk, leg.pk)
