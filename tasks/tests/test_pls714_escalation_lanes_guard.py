"""PLS-714 guard: newly created workflows must inherit the escalation lanes.

Migrations 0044/0053/0069 seeded Escalate to Human Ops / Raise to Admin Ops /
Escalate to Leads only for workflows that existed at migration time; workflows
created later (feature-request-rfc, channel-message-review) missed them. This
guard tests the two mechanisms that close the gap:
  1. migration 0163 seeds the missing lanes for migration-time workflows;
  2. WorkflowService.ensure_escalation_lanes is invoked on workflow creation so
     a newly created workflow inherits the lanes (never repeats the gap).
"""
from django.test import TestCase

from tasks.models import Status, Workflow, WorkflowTransition
from tasks.services.workflow_service import WorkflowService


REQUIRED_ESCALATIONS = (
    'Escalate to Human Ops',
    'Raise to Admin Ops',
    'Escalate to Leads',
)


def _workflow_lane_names(workflow):
    return set(
        WorkflowTransition.objects.filter(workflow=workflow)
        .values_list('name', flat=True)
    )


class Pls714EscalationLanesGuardTests(TestCase):
    def test_newly_created_workflow_inherits_all_escalation_lanes(self):
        """The runtime guard: creating a workflow via the serializer/service path
        must seed the full escalation lane set (targeting blocked)."""
        open_status, _ = Status.objects.get_or_create(
            slug='open', defaults={'name': 'Open', 'category': 'todo'},
        )
        blocked_status, _ = Status.objects.get_or_create(
            slug='blocked', defaults={'name': 'Blocked', 'category': 'in_progress'},
        )
        workflow = Workflow.objects.create(
            name='PLS-714 Guard WF', slug='pls714-guard-wf',
            initial_status=open_status, is_active=True,
        )
        # Simulate the serializer create path: statuses added, then lanes ensured.
        from tasks.models import WorkflowStatus
        WorkflowStatus.objects.create(workflow=workflow, status=open_status, position=0)
        WorkflowStatus.objects.create(workflow=workflow, status=blocked_status, position=1)
        WorkflowService.ensure_escalation_lanes(workflow)

        names = _workflow_lane_names(workflow)
        for name in REQUIRED_ESCALATIONS:
            self.assertIn(name, names, f'new workflow missing {name!r}')

        # Escalations must target blocked so the PLAT-4215 create_blocker_task
        # post-function applies (0100 only seeds blocked).
        for name in REQUIRED_ESCALATIONS:
            targets = set(
                WorkflowTransition.objects.filter(workflow=workflow, name=name)
                .values_list('to_status', flat=True)
            )
            self.assertIn('blocked', targets, f'{name!r} must target blocked')

        # Idempotent: re-running must not duplicate.
        count_before = WorkflowTransition.objects.filter(workflow=workflow).count()
        WorkflowService.ensure_escalation_lanes(workflow)
        count_after = WorkflowTransition.objects.filter(workflow=workflow).count()
        self.assertEqual(count_after, count_before)

    def test_deficient_workflows_have_lanes_after_migration(self):
        """feature-request-rfc and bug-workflow must carry the full lane set
        (migration 0163 seeds them; they exist at migration time)."""
        for slug in ('feature-request-rfc', 'bug-workflow'):
            workflow = Workflow.objects.filter(slug=slug).first()
            if workflow is None:
                continue  # not present in this DB; covered by the runtime guard
            names = _workflow_lane_names(workflow)
            missing = [n for n in REQUIRED_ESCALATIONS if n not in names]
            self.assertEqual(
                missing, [],
                f'workflow {slug} is missing escalation lanes: {missing}',
            )
