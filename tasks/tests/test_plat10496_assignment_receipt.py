"""PLAT-10496: a concrete clear posts one thread receipt, and a rollback posts none."""
from django.db import connection, transaction
from django.test import TestCase

from tasks.models import Comment, Item, Project
from tasks.services.routing_authority import (
    clear_assignment_to_group_queue,
    emit_assignment_clear_thread_receipt,
)
from tasks.models import RoutingTeam


class AssignmentClearReceiptTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Receipt', key='RCPT', org_id=1, access_level='open', is_active=True,
        )
        RoutingTeam.objects.create(
            organization_id=1, key='engineering', display_name='Engineering',
            state='active', version=1, digest='a' * 64,
        )
        self.item = Item.objects.create(
            mode='task', title='owned', project=self.project,
            workflow_name='simple', assignment_group='engineering',
        )
        # The assignment trigger rejects an owner write without a consumed
        # receipt. Seed the prior owner with triggers off; the clear command
        # under test still arms its own receipt and posts the thread line.
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL session_replication_role = 'replica'")
            Item.objects.filter(pk=self.item.pk).update(
                assignee_id=9, assignee_email='ren@shizuha.com',
            )
            cursor.execute("SET LOCAL session_replication_role = 'origin'")
        self.item.refresh_from_db()

    def _cleared(self):
        return Comment.objects.filter(item=self.item, origin_event='assignment.cleared')

    def test_concrete_clear_posts_one_receipt_and_retry_does_not_double_post(self):
        clear_assignment_to_group_queue(self.item, reason='explicit_unassign')
        self.item.refresh_from_db()
        self.assertIsNone(self.item.assignee_id)
        self.assertEqual(self._cleared().count(), 1)
        proof = type('Proof', (), {
            'assignee_email': 'ren@shizuha.com',
            'reason': 'explicit_unassign',
            'action': 'clear',
            'result_assignment_group': 'engineering',
        })()
        emit_assignment_clear_thread_receipt(self.item, proof)
        self.assertEqual(self._cleared().count(), 1)

    def test_rolled_back_clear_posts_no_receipt(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                clear_assignment_to_group_queue(self.item, reason='explicit_unassign')
                raise RuntimeError('abort clear')
        self.item.refresh_from_db()
        self.assertEqual(self.item.assignee_email, 'ren@shizuha.com')
        self.assertEqual(self._cleared().count(), 0)
