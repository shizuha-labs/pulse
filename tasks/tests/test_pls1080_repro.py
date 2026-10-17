"""PLS-1080 reproduction: Approve on workflow-change STILL fails (shizuha != shizuha).

Mirrors the PLAT-4806/4807 Approve transition: a shizuha-org task whose Approve
carries post-function assign_team:engineering. Engineering HAS an active
org-shizuha roster row, yet the org guard reports (shizuha != shizuha).
"""
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService


def _organization(org_id):
    return {
        1: {'id': 1, 'slug': 'shizuha'},
        2: {'id': 2, 'slug': 'shizuha-digital'},
        3: {'id': 3, 'slug': 'shizuha-trading'},
        4: {'id': 4, 'slug': 'personal-3'},
    }.get(org_id)


class Pls1080EngineeringApproveRepro(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Platform', key='PLSHZ', org_id=1,
        )
        self.item = Item.objects.create(
            project=self.project, item_key='PLSHZ-1', mode='task',
            title='Workflow change', assignment_group='engineering',
        )
        # Engineering roster in the shizuha org (active) — the production shape.
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={
                'tiers': [['ryo@shizuha.com', 'jun@shizuha.com', 'kai@shizuha.com', 'sara@shizuha.com']],
                'is_active': True,
            },
        )

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_engineering_member_authorized_same_org(self, _):
        """A real engineering member on a shizuha-org task must NOT mismatch."""
        mismatch, scope, task_org = WorkflowService.assignment_org_mismatch(
            self.item, 'jun@shizuha.com', 'engineering',
        )
        self.assertFalse(mismatch, (mismatch, scope, task_org))
        self.assertEqual(task_org, 'shizuha')

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_team_roster_entry_for_item_engineering(self, _):
        """_team_roster_entry_for_item must find the engineering shizuha row."""
        tiers, org = WorkflowService._team_roster_entry_for_item(
            'engineering', self.item,
        )
        self.assertIsNotNone(tiers)
        self.assertEqual(org, 'shizuha')

    @patch(
        'tasks.services.admin_service.admin_service.get_organization',
        side_effect=_organization,
    )
    def test_resolved_assignee_not_in_tiers_masks_as_same_org(self, _):
        """If the resolved assignee is NOT an exact engineering member but IS in
        the shizuha org roster, the guard returns (shizuha != shizuha) instead
        of the not-in-team sentinel — masking the real reason."""
        # A shizuha-org member who is NOT in the engineering tiers.
        TeamRoster.objects.update_or_create(
            team_name='security', organization_slug='shizuha',
            defaults={'tiers': [['akira@shizuha.com']], 'is_active': True},
        )
        mismatch, scope, task_org = WorkflowService.assignment_org_mismatch(
            self.item, 'akira@shizuha.com', 'engineering',
        )
        self.assertTrue(mismatch)
        # PLS-1080 fix: the truthful sentinel is returned, NOT the masked
        # (shizuha != shizuha) same-org shape.
        self.assertEqual(scope, 'not-in-team')
        self.assertEqual(task_org, 'shizuha')
        # assignment_denial_reason renders the truthful reason.
        reason = WorkflowService.assignment_denial_reason(
            'akira@shizuha.com', scope, task_org,
        )
        print(f'REASON: {reason}')
        self.assertIn('not in the task organization', reason)
