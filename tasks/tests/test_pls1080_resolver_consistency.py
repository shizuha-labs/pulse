"""PLS-1080 regression: the resolver and the assignment guard must resolve the
SAME TeamRoster row for a slug-only team (organization_id=NULL).

The Approve transition's assign_team:engineering post-function resolves a
concrete assignee via _resolve_team_assignee -> routing_candidate_snapshot_for_item
(keys by organization_id). The org guard validates via _team_roster_entry_for_item
(keys by organization_slug first, then id). A slug-only row (organization_id=NULL)
was invisible to the resolver while visible to the guard — the two sides diverged
on the exact engineering roster, so a resolved member could be rejected by the
guard as not-in-team (surfacing as the untruthful ``shizuha != shizuha``).

Fix: routing_candidate_snapshot_for_item falls back to the slug-keyed lookup,
mirroring _team_roster_entry_for_item.
"""
from unittest.mock import patch

from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.routing_authority import routing_candidate_snapshot_for_item
from tasks.services.workflow_service import WorkflowService


def _organization(org_id):
    return {
        1: {'id': 1, 'slug': 'shizuha'},
        2: {'id': 2, 'slug': 'shizuha-digital'},
        3: {'id': 3, 'slug': 'shizuha-trading'},
        4: {'id': 4, 'slug': 'personal-3'},
    }.get(org_id)


class Pls1080ResolverConsistencyTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Shizuha Platform', key='PLSHZ', org_id=1,
            access_level='open',
        )
        self.item = Item.objects.create(
            project=self.project, item_key='PLSHZ-1', mode='task',
            title='Workflow change', assignment_group='engineering',
        )

    @patch('tasks.services.admin_service.admin_service.get_organization',
           side_effect=_organization)
    def test_snapshot_falls_back_to_slug_keyed_row(self, _):
        """A slug-only engineering row (organization_id=NULL) must be visible to
        the resolver's snapshot, matching the guard's _team_roster_entry_for_item."""
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={'tiers': [['jun@shizuha.com']], 'is_active': True,
                      'organization_id': None},
        )
        snap = routing_candidate_snapshot_for_item('engineering', self.item)
        self.assertEqual(snap['source'], 'team_roster_compat')
        self.assertEqual(snap['tiers'], [['jun@shizuha.com']])
        self.assertEqual(snap['organization_slug'], 'shizuha')

        # The guard sees the same row.
        tiers, org = WorkflowService._team_roster_entry_for_item(
            'engineering', self.item,
        )
        self.assertEqual(tiers, [['jun@shizuha.com']])
        self.assertEqual(org, 'shizuha')

        # A member of the slug-only row is authorized by the guard.
        mismatch, scope, task_org = WorkflowService.assignment_org_mismatch(
            self.item, 'jun@shizuha.com', 'engineering',
        )
        self.assertFalse(mismatch, (mismatch, scope, task_org))

    @patch('tasks.services.admin_service.admin_service.get_organization',
           side_effect=_organization)
    def test_snapshot_id_keyed_row_still_wins(self, _):
        """When the row is keyed by organization_id, the id lookup still wins
        (no behavior change for the common migrated shape)."""
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_slug='shizuha',
            defaults={'tiers': [['jun@shizuha.com']], 'is_active': True,
                      'organization_id': 1},
        )
        snap = routing_candidate_snapshot_for_item('engineering', self.item)
        self.assertEqual(snap['tiers'], [['jun@shizuha.com']])
        self.assertEqual(snap['organization_slug'], 'shizuha')
