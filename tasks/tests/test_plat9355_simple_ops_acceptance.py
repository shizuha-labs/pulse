"""PLAT-9355: simple (ops/no-PR) workflow acceptance-mode exemption + reclassification.

Two-layer defect fixed:
1. The system ``simple`` workflow has no PR/merge lane, so the deploy-verify
   gate's merged-head DeploymentEvent demand is structurally unsatisfiable —
   yet the create-time default resolved service-affecting/user-facing ops
   tasks to ``live``, stranding every operational fix at the done transition
   (HIVE-2648 was blocked 2x). The resolution now exempts the system simple
   workflow BEFORE the raw-field short-circuit (persisted-live rows are
   reachable), and the gate carries the same exemption as a durable net.
2. ``acceptance_mode`` is read-only on update (PLS-1249) with no agent-
   reachable reclassification path — this adds the authorized path the
   serializer's 400 message names: a role-gated (devops/admin-ops) service
   method + ``POST /items/{pk}/reclassify-acceptance-mode/`` with a mandatory
   reason and an auditable mechanical comment.
"""
from unittest import mock

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from django.contrib.auth.models import User

from tasks.models import Comment, Item, Project, TeamRoster
from tasks.services.workflow_service import (
    WorkflowService,
    WorkflowValidationError,
)


class SimpleOpsAcceptanceResolutionTests(TestCase):
    """Layer 1: the resolution + gate exemptions for the system simple workflow."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Platform Ops', key='PLATOPS', org_id=1,
            access_level='private', is_active=True,
        )
        patcher = mock.patch(
            'tasks.liveness.record_heartbeat', return_value=1700000000.0,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _item(self, *, workflow_name='simple', labels=None, tags='',
              acceptance_mode=None, status='in_review', **kw):
        create_kw = dict(
            project=self.project,
            title='[ops] reprovision QA fixture claw',
            description='Operational fix, no PR lane.',
            labels=labels if labels is not None else [],
            tags=tags,
            status=status,
            workflow_name=workflow_name,
            item_type='task',
            created_by_id=1,
            created_by_email='ops@shizuha.com',
            **kw,
        )
        if acceptance_mode is not None:
            create_kw['acceptance_mode'] = acceptance_mode
        return Item.objects.create(**create_kw)

    def test_simple_ops_with_user_facing_labels_resolves_code(self):
        # The HIVE-2648 shape: ops task carrying user-facing /
        # infra-control-plane risk labels on the simple workflow.
        item = self._item(labels=['user-facing'], tags='infra-control-plane')
        self.assertEqual(
            WorkflowService._resolve_acceptance_mode(item), 'code',
        )

    def test_simple_ops_persisted_live_field_is_reachable(self):
        # Rows whose field was persisted ``live`` (create-time default or the
        # 0149 backfill) must be exempted too — the arm sits BEFORE the
        # raw-field short-circuit, mirroring PLAT-7600.
        item = self._item(
            labels=['service-affecting'], acceptance_mode='live',
        )
        self.assertEqual(item.acceptance_mode, 'live')
        self.assertEqual(
            WorkflowService._resolve_acceptance_mode(item), 'code',
        )

    def test_pr_driven_workflow_same_labels_still_live(self):
        # Negative control: the exemption is the system workflow slug, not a
        # label heuristic — deploy-governed workflows keep the live default.
        item = self._item(
            workflow_name='autonomous-bug', labels=['service-affecting'],
        )
        self.assertEqual(
            WorkflowService._resolve_acceptance_mode(item), 'live',
        )

    def test_simple_ops_plain_item_resolves_code(self):
        item = self._item(labels=[])
        self.assertEqual(
            WorkflowService._resolve_acceptance_mode(item), 'code',
        )

    def test_gate_collects_no_deploy_evidence_for_simple_ops(self):
        # Durable safety net: even with the field persisted live, the gate
        # collects no deploy-evidence classes for a simple-ops item.
        item = self._item(
            labels=['user-facing'], acceptance_mode='live',
        )
        self.assertEqual(
            WorkflowService._collect_deploy_verify_missing(item, 'live'), [],
        )

    def test_gate_still_demands_evidence_on_pr_driven_workflow(self):
        # Negative control for the gate arm.
        item = self._item(
            workflow_name='autonomous-bug', labels=['service-affecting'],
            acceptance_mode='live', status='in_review',
        )
        missing = WorkflowService._collect_deploy_verify_missing(item, 'live')
        self.assertTrue(missing)


class AcceptanceReclassificationServiceTests(TestCase):
    """Layer 2a: the role-gated reclassification service method."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Platform Ops', key='PLATOP2', org_id=1,
            access_level='private', is_active=True,
        )
        patcher = mock.patch(
            'tasks.liveness.record_heartbeat', return_value=1700000000.0,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.item = Item.objects.create(
            project=self.project,
            title='[ops] misapplied live acceptance',
            description='Simple-workflow ops task stranded at close.',
            labels=['user-facing'],
            tags='',
            status='in_review',
            workflow_name='simple',
            item_type='task',
            created_by_id=1,
            created_by_email='ops@shizuha.com',
            acceptance_mode='live',
        )

    def _roster(self, team, email):
        TeamRoster.objects.get_or_create(
            organization_id=self.project.org_id,
            team_name=team,
            defaults={
                'organization_slug': f'test-org-{self.project.org_id}',
                'tiers': [[email]],
            },
        )

    def test_reclassify_requires_authorized_team(self):
        engineer = mock.Mock(email='eng@shizuha.com')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.reclassify_governed_acceptance_mode(
                self.item, actor=engineer, new_mode='code',
                reason='ops work has no deploy surface',
            )

    def test_reclassify_devops_member_succeeds_and_audits(self):
        email = 'devops@shizuha.com'
        actor = User.objects.create_user(
            username='plat9355-devops', email=email, password='x',
        )
        # PLS-524: a seeded devops binding is authoritative and ignores a
        # TeamRoster row written only for this test.
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            returned = WorkflowService.reclassify_governed_acceptance_mode(
                self.item, actor=actor, new_mode='code',
                reason='simple workflow has no merge lane',
            )
        self.assertEqual(returned.acceptance_mode, 'code')
        self.item.refresh_from_db()
        self.assertEqual(self.item.acceptance_mode, 'code')
        audit = Comment.objects.filter(item=self.item).order_by('-id').first()
        self.assertIsNotNone(audit)
        self.assertIn('acceptance_mode_reclassification', audit.origin_event or '')

    def test_reclassify_rejects_unknown_mode(self):
        self._roster('devops', 'devops2@shizuha.com')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.reclassify_governed_acceptance_mode(
                self.item, actor=mock.Mock(email='devops2@shizuha.com'),
                new_mode='yolo', reason='why not',
            )

    def test_reclassify_rejects_empty_reason(self):
        self._roster('devops', 'devops3@shizuha.com')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.reclassify_governed_acceptance_mode(
                self.item, actor=mock.Mock(email='devops3@shizuha.com'),
                new_mode='code', reason='   ',
            )

    def test_reclassify_rejects_automation_identity(self):
        self._roster('devops', 'system@shizuha.com')
        with self.assertRaises(WorkflowValidationError):
            WorkflowService.reclassify_governed_acceptance_mode(
                self.item, actor=mock.Mock(email='system@shizuha.com'),
                new_mode='code', reason='automation bypass',
            )


class AcceptanceReclassificationEndpointTests(APITestCase):
    """Layer 2b: POST /items/{pk}/reclassify-acceptance-mode/."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username='reclasser',
            email='reclasser@example.com',
            password='testpass123',
            is_staff=True,
        )
        self.client.force_authenticate(user=self.user)
        self.project = Project.objects.create(
            name='Platform Ops', key='PLATOP3', org_id=1,
            access_level='private', is_active=True,
        )
        self.item = Item.objects.create(
            project=self.project,
            title='[ops] endpoint reclassification target',
            description='Simple-workflow ops task.',
            labels=['user-facing'],
            tags='',
            status='in_review',
            workflow_name='simple',
            item_type='task',
            created_by_id=1,
            created_by_email='ops@shizuha.com',
            acceptance_mode='live',
        )
        self.url = f'/api/items/{self.item.pk}/reclassify-acceptance-mode/'

    def test_unknown_mode_is_400(self):
        resp = self.client.post(
            self.url, {'acceptance_mode': 'yolo', 'reason': 'r'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_empty_reason_is_400(self):
        resp = self.client.post(
            self.url, {'acceptance_mode': 'code', 'reason': ''},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthorized_team_is_403(self):
        resp = self.client.post(
            self.url,
            {'acceptance_mode': 'code', 'reason': 'no roster membership'},
            format='json',
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_authorized_reclassification_is_200(self):
        # Membership is binding-authoritative when a snapshot exists (PLS-524).
        # A TeamRoster row does not override a seeded admin-ops binding, so
        # the HTTP contract is proven with the same gate the service uses.
        with mock.patch.object(
            WorkflowService, '_is_team_member', return_value=True,
        ):
            resp = self.client.post(
                self.url,
                {'acceptance_mode': 'code', 'reason': 'ops work, no deploy surface'},
                format='json',
            )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.item.refresh_from_db()
        self.assertEqual(self.item.acceptance_mode, 'code')
