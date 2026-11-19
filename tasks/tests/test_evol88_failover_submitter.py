"""EVOL-88: review-lane failover must not seat the submitter."""
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from tasks.models import Item, Project, Status, TeamRoster, Workflow, WorkflowStatus
from tasks.services.workflow_service import WorkflowService
from tasks.tasks import (
    failover_review_submitter_block,
    sweep_rebalance_pods,
)


class FailoverSubmitterBlockTests(SimpleTestCase):
    def test_review_lane_blocks_submitter(self):
        item = SimpleNamespace(status='in_review', created_by_email='kumo@shizuha.com')
        with patch(
            'tasks.services.workflow_service.WorkflowService._emails_same_agent_identity',
            side_effect=lambda a, b: a.lower() == b.lower(),
        ):
            self.assertEqual(
                failover_review_submitter_block(item, 'kumo@shizuha.com', {'in_review'}),
                'reviewer_is_submitter',
            )

    def test_hands_on_lane_does_not_block_submitter(self):
        item = SimpleNamespace(status='in_progress', created_by_email='kumo@shizuha.com')
        self.assertIsNone(
            failover_review_submitter_block(item, 'kumo@shizuha.com', {'in_review'}),
        )

    def test_other_reviewer_is_not_blocked(self):
        item = SimpleNamespace(status='in_review', created_by_email='kumo@shizuha.com')
        with patch(
            'tasks.services.workflow_service.WorkflowService._emails_same_agent_identity',
            return_value=False,
        ):
            self.assertIsNone(
                failover_review_submitter_block(item, 'rui@shizuha.com', {'in_review'}),
            )


class PLAT4808ReplayTests(TestCase):
    """PLAT-4808 replay: review-lane failover must never seat the submitter.

    Specimen: 6 consecutive failovers each picked the item's submitter, the
    transition validator refused each with reviewer!=submitter, and the item
    stranded in a failover loop. The sweep-level predicate
    (failover_review_submitter_block) must make that loop structurally
    impossible: the submitter is skipped with the reason recorded BEFORE any
    seat is chosen, so a submitter-only candidate pool leaves the item in
    place instead of churning it.
    """

    @classmethod
    def _fixture(cls, *, with_rescue_seat: bool):
        import uuid as _uuid
        tag = _uuid.uuid4().hex[:10]
        owner = f'downowner{tag}@shizuha.com'      # goes unavailable mid-review
        submitter = f'submitter{tag}@shizuha.com'  # filed the item
        rescue = f'rescuer{tag}@shizuha.com' if with_rescue_seat else None
        base = 480000 + (int(tag, 16) % 100000)
        org_id = 480000 + (int(tag, 16) % 30000)
        members = [owner, submitter] + ([rescue] if rescue else [])
        tiers = [members]
        users = {}
        for i, m in enumerate(members):
            users[m] = (base + i, m)
        TeamRoster.objects.create(
            team_name='review',
            organization_slug=f'torg{org_id}',
            organization_id=org_id,
            tiers=tiers,
            is_active=True,
        )
        project = Project.objects.create(
            org_id=org_id, name=f'Review {org_id}', key=f'RV{org_id % 1000:03d}',
        )
        # Simple workflow has no in_review slug. Item.save coerces that to the
        # same-category status (in_progress) before the sweep runs, so the
        # status assertion was measuring workflow attach, not failover.
        review_status, _ = Status.objects.get_or_create(
            slug='in_review',
            defaults={'name': 'In Review', 'category': 'in_progress', 'position': 4},
        )
        open_status, _ = Status.objects.get_or_create(
            slug='open',
            defaults={'name': 'Open', 'category': 'todo', 'position': 1},
        )
        workflow = Workflow.objects.create(
            name=f'EVOL88 {tag}',
            slug=f'evol88-{tag}',
            initial_status=open_status,
        )
        WorkflowStatus.objects.create(workflow=workflow, status=open_status, position=0)
        WorkflowStatus.objects.create(workflow=workflow, status=review_status, position=1)
        item = Item.objects.create(
            title='PLAT-4808 specimen: review-lane item mid-pipeline',
            mode='task', status='in_review', priority='urgent',
            project=project,
            workflow_name=workflow.name,
            assignment_group='review',
            assignee_id=users[owner][0], assignee_email=owner,
            created_by_email=submitter,  # Item has no reporter field; the predicate falls through to created_by_email
        )
        return owner, submitter, rescue, users, item

    def _run(self, *, owner, users, live):
        from tasks.tests.test_hibernated_member_rebalance import (
            _ENV,
            _fake_user_service,
        )
        with patch.dict(os.environ, _ENV, clear=False), \
             patch('tasks.user_service.user_service', _fake_user_service(users)), \
             patch.object(
                 WorkflowService, '_is_agent_available_for_assignment',
                 side_effect=lambda e: e.lower() in live,
             ), \
             patch.object(
                 WorkflowService, '_hibernated_agent_emails',
                 return_value={owner},
             ), \
             patch('tasks.tasks._agent_unavailability_reason', return_value='hibernated'), \
             patch('tasks.tasks._agent_has_recent_pulse_heartbeat', return_value=True):
            return sweep_rebalance_pods.run()

    def test_sweep_never_seats_the_submitter(self):
        owner, submitter, rescue, users, item = self._fixture(
            with_rescue_seat=False,
        )
        # Only the submitter survives — the PLAT-4808 trap pool.
        self._run(owner=owner, users=users, live={submitter})
        item.refresh_from_db()
        self.assertNotEqual(
            (item.assignee_email or '').lower(), submitter.lower(),
            'sweep seated the review submitter — PLAT-4808 loop regressed',
        )
        self.assertEqual(item.status, 'in_review')  # not churned into another lane state

    def test_sweep_lands_on_eligible_non_submitter(self):
        owner, submitter, rescue, users, item = self._fixture(
            with_rescue_seat=True,
        )
        self._run(owner=owner, users=users, live={rescue})
        item.refresh_from_db()
        self.assertEqual(
            (item.assignee_email or '').lower(), rescue.lower(),
            'eligible non-submitter teammate should receive the failed-over item',
        )
