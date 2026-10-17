"""PLAT-9682: typed-escalation channel health canary.

The canary must exercise the REAL forced-hold commit machinery (the path that
raised ``escalation_hold_commit_failed`` 3x under PLAT-9678 while health was
green) on a dedicated canary carrier, WITHOUT routing anything to admin-ops
or paging a human, and fire a deduplicated critical alert with the named
fallback channel when the commit leg fails.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from tasks.models import (
    EscalationRoutingReceipt, Item, ItemLink, Project, Status,
    Workflow, WorkflowStatus, WorkflowTransition,
)
from tasks.services import escalation_canary_probe
from tasks.services.escalation_canary_probe import (
    CANARY_REASON_CODE, CANARY_TITLE, run_escalation_canary,
)


@override_settings(PULSE_ESCALATION_CANARY_ACTOR_EMAIL='canary-actor@example.com')
class EscalationCanaryProbeTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.actor = user_model.objects.create_user(
            username='canary-actor',
            email='canary-actor@example.com',
            is_staff=True,
        )
        self.project = Project.objects.create(name='PLAT-9682', key='P682', org_id=1)
        self.canary = escalation_canary_probe
        settings_ctx = override_settings(
            PULSE_ESCALATION_CANARY_PROJECT_ID=self.project.pk,
        )
        settings_ctx.enable()
        self.addCleanup(settings_ctx.disable)

    def _workflow(self):
        open_status, _ = Status.objects.get_or_create(
            slug='open', defaults={'name': 'Open', 'category': 'todo'},
        )
        blocked_status, _ = Status.objects.get_or_create(
            slug='blocked', defaults={'name': 'Blocked', 'category': 'in_progress'},
        )
        workflow = Workflow.objects.create(
            name='canary wf', slug=self.canary.CANARY_WORKFLOW_SLUG,
            initial_status=open_status, is_active=True,
        )
        WorkflowStatus.objects.create(workflow=workflow, status=open_status, position=0)
        WorkflowStatus.objects.create(workflow=workflow, status=blocked_status, position=1)
        WorkflowTransition.objects.create(
            workflow=workflow, name='Raise to Admin Ops (canary)',
            from_status='open', to_status='blocked',
        )
        return workflow

    def _manual_item(self, workflow, key):
        from tasks.services.workflow_service import WorkflowService
        with mock.patch.object(
            WorkflowService, 'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ):
            return self._make_item(workflow, key)

    def _make_item(self, workflow, key):
        return Item.objects.create(
            project=self.project, item_key=key, mode='task',
            title=CANARY_TITLE, status='open', workflow_status='open',
            workflow_name=workflow.slug,
            assignment_group='engineering',
            assignee_id=self.actor.id, assignee_email=self.actor.email,
            created_by_id=self.actor.id, created_by_email=self.actor.email,
        )

    def _run(self):
        """Run the canary with the live Admin-org authority boundary kept
        deterministic (organization membership / item access are enforced by
        the real Admin API, intentionally unavailable in this isolated test —
        same patch class the PLS-525 hold tests use)."""
        from tasks.services import permissions as permission_service
        from tasks.services.workflow_service import WorkflowService
        with mock.patch.object(
            WorkflowService, 'assignment_org_mismatch',
            return_value=(False, 'shizuha', 'shizuha'),
        ), mock.patch.object(
            permission_service.PermissionService, 'identity_belongs_to_organization',
            return_value=True,
        ), mock.patch.object(
            permission_service.PermissionService, 'can_access_item',
            return_value=True,
        ):
            return run_escalation_canary()

    def test_green_run_commits_hold_without_admin_ops_routing(self):
        run = self._run()

        self.assertEqual(run['outcome'], 'green')
        self.assertFalse(run['alert_fired'])
        item = Item.objects.get(project=self.project, title=CANARY_TITLE)
        receipt = EscalationRoutingReceipt.objects.filter(item=item).order_by('-id').first()
        self.assertIsNotNone(receipt)
        self.assertEqual(run['receipt_pk'], receipt.pk)
        self.assertGreaterEqual(run['disposition_generation'], 1)
        # Preserve-without-routing: no blocker child anywhere, carrier stays
        # open. Never an admin-ops task, never a human page, never the
        # retired waiting_external status.
        self.assertEqual(
            ItemLink.objects.filter(link_type='blocks').count(), 0,
        )
        self.assertEqual(item.status, 'open')
        self.assertNotEqual(item.status, 'waiting_external')
        # Self-provisioning produced the canary workflow with no post-functions.
        wf = Workflow.objects.get(slug=self.canary.CANARY_WORKFLOW_SLUG)
        transition = WorkflowTransition.objects.get(workflow=wf)
        self.assertEqual(transition.from_status, 'open')
        self.assertEqual(transition.to_status, 'blocked')

    def test_second_run_is_idempotent_and_advances_generation(self):
        # The release leg internally ranks the owner queue with postgres-only
        # JSONField lookups; the focus-check outcome (no higher-impact blocker
        # for a canary-only org) is a boundary stub here — the release
        # machinery itself is covered by the external-wait suites.
        from tasks.services.workflow_service import WorkflowService
        with mock.patch.object(
            WorkflowService, '_higher_impact_owned_blocker', return_value=None,
        ):
            first = self._run()
            second = self._run()

        self.assertEqual(first['outcome'], 'green')
        self.assertEqual(second['outcome'], 'green')
        self.assertNotEqual(first['request_id'], second['request_id'])
        self.assertGreater(
            second['disposition_generation'], first['disposition_generation'],
        )
        item = Item.objects.get(project=self.project, title=CANARY_TITLE)
        self.assertEqual(
            EscalationRoutingReceipt.objects.filter(item=item).count(), 2,
        )

    def test_commit_failure_fires_critical_alert_with_named_fallback(self):
        with mock.patch(
            'tasks.services.escalation_routing.EscalationRoutingService._execute_forced_hold',
            side_effect=RuntimeError('simulated commit failure'),
        ) as hold, mock.patch(
            'tasks.services.deduplication.AlertDeduplicationService.submit_alert',
        ) as submit:
            run = self._run()

        self.assertEqual(run['outcome'], 'commit_failed')
        self.assertTrue(run['alert_fired'])
        hold.assert_called_once()
        # The canary reason code must route NOWHERE: preserve-without-routing.
        self.assertEqual(hold.call_args.kwargs.get('reason_code'), CANARY_REASON_CODE)
        self.assertTrue(submit.called)
        alert = submit.call_args[0][0]
        self.assertEqual(alert['severity'], 'critical')
        self.assertIn('escalation_hold_commit_failed', alert['title'])
        self.assertIn('admin-ops owner', alert['description'])
        self.assertIn('PLAT-9682', alert['description'])

    def test_missing_receipt_after_quiet_commit_is_treated_as_failure(self):
        with mock.patch(
            'tasks.services.escalation_routing.EscalationRoutingService._execute_forced_hold',
            side_effect=lambda *a, **k: Item.objects.get(
                project=self.project, title=CANARY_TITLE),
        ), mock.patch(
            'tasks.services.deduplication.AlertDeduplicationService.submit_alert',
        ) as submit:
            run = self._run()

        self.assertEqual(run['outcome'], 'commit_failed')
        self.assertEqual(run.get('phase'), 'verify')
        self.assertTrue(submit.called)

    def test_unconfigured_project_fires_warning_and_never_commits(self):
        with override_settings(PULSE_ESCALATION_CANARY_PROJECT_ID=None), mock.patch(
            'tasks.services.deduplication.AlertDeduplicationService.submit_alert',
        ) as submit:
            run = self._run()

        self.assertEqual(run['outcome'], 'unconfigured')
        self.assertTrue(run['alert_fired'])
        self.assertFalse(EscalationRoutingReceipt.objects.exists())
        alert = submit.call_args[0][0]
        self.assertEqual(alert['severity'], 'warning')
        self.assertIn('unmonitored', alert['title'])

    def test_open_carrier_recommits_without_a_wait_release(self):
        # A green run leaves the carrier open. waiting_external is retired,
        # so the next run does not need a typed release before recommitting.
        first = self._run()
        self.assertEqual(first['outcome'], 'green')
        item = Item.objects.get(project=self.project, title=CANARY_TITLE)
        self.assertEqual(item.status, 'open')
        self.assertNotEqual(item.status, 'waiting_external')

        with mock.patch(
            'tasks.services.external_wait.release_human_wait',
            return_value={'released': True},
        ) as release, mock.patch.object(
            escalation_canary_probe, '_pick_transition',
            return_value=None,
        ):
            second = self._run()

        release.assert_not_called()
        self.assertEqual(second['outcome'], 'no_transition')
