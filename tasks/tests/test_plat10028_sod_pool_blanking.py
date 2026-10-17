"""PLAT-10028: merge SoD must not blank the merger pool on lookup failure.

Two regression faces from the GUILD-7 wedge (awaiting_merge with a fresh
exact-head APPROVED sat unassigned for hours while distinct merger candidates
existed):

1. A corrupt ``PULSE_SCM_IDENTITIES_JSON`` made ``_agent_scm_identity``
   return ``None`` for every candidate, so the exclusion loop marked the whole
   merge roster ``unknown`` and minted ``no_distinct_available_merger`` on a
   provably non-empty pool.
2. Any review-lookup error (``evidence_errors``) hard-excluded every roster
   candidate regardless of identity, so one transient Forgejo timeout (or a
   persistent token/base misconfig) blanked the pool and minted
   ``sod_evidence_unavailable`` — even though last-known hold identities
   already proved which seats were conflicting.
"""
from unittest import mock

from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService


class ScmIdentityFallbackTests(TestCase):
    def test_corrupt_identity_map_falls_back_to_local_part(self):
        """PLAT-10028 face 1: invalid map JSON must not disable resolution."""
        with mock.patch.dict(
            'os.environ',
            {'PULSE_SCM_IDENTITIES_JSON': '{not json'},
            clear=False,
        ):
            self.assertEqual(
                WorkflowService._agent_scm_identity('kei@shizuha.com'), 'kei',
            )
            self.assertEqual(
                WorkflowService._agent_scm_identity('revi@shizuha.com'), 'revi',
            )


class EvidenceErrorExclusionTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='PLAT10028', key='P10028', org_id=1)
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name='merge',
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [[
                    'aoi@shizuha.com',
                    'kei@shizuha.com',
                    'ryo@shizuha.com',
                    'haru@shizuha.com',
                ]],
                'is_active': True,
            },
        )

    def _item(self, hold):
        return Item.objects.create(
            title='approved PR wedged by evidence error',
            project=self.project,
            mode='task',
            workflow_name='autonomous-dev',
            status='awaiting_merge',
            workflow_status='awaiting_merge',
            assignment_group='merge',
            metadata={
                'pull_requests': [
                    {'url': 'https://origin.shizuha.com/shizuha-labs/guild/pulls/8'},
                ],
                'merge_sod_hold': hold,
            },
        )

    def test_evidence_error_excludes_only_last_known_conflicts(self):
        """PLAT-10028 face 2: lookup failure must not blank the pool."""
        item = self._item({
            'reason': 'sod_evidence_unavailable',
            'current_pr_authors': ['jun'],
            'current_head_reviewers': ['revi'],
            'excluded_assignees': [],
            'unknown_identity_assignees': [],
        })
        identities = {
            'aoi@shizuha.com': 'aoi',
            'kei@shizuha.com': 'kei',
            'ryo@shizuha.com': 'ryo',
            'haru@shizuha.com': 'haru',
        }
        captured = {}

        def resolve(team, **kwargs):
            captured['hard_exclude_emails'] = kwargs['hard_exclude_emails']
            return self.project.id, 'aoi@shizuha.com'

        with mock.patch.object(
            WorkflowService,
            '_linked_origin_pr_authors',
            return_value=({'jun'}, [], 1),
        ), mock.patch.object(
            WorkflowService,
            '_linked_pr_current_head_reviewers',
            return_value=(set(), ['forgejo_review_lookup_failed:ConnectionError'], 1),
        ), mock.patch.object(
            WorkflowService,
            '_agent_github_identity',
            side_effect=lambda email: identities.get((email or '').lower()),
        ), mock.patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=resolve,
        ):
            lane, changed = WorkflowService._enforce_status_lane_owner(
                item, item.status, [],
            )

        self.assertEqual(lane, 'merge')
        self.assertTrue(changed)
        # Distinct candidates were NOT collateral-excluded by the error.
        self.assertEqual(captured['hard_exclude_emails'], set())
        self.assertEqual(item.assignee_email, 'aoi@shizuha.com')
        # Assignment succeeded, so the stale hold must be cleared, not re-minted.
        self.assertNotIn('merge_sod_hold', item.metadata or {})

    def test_evidence_error_still_excludes_last_known_reviewer(self):
        """SoD floor: the last-known reviewer seat stays excluded on failure."""
        item = self._item({
            'reason': 'no_distinct_available_merger',
            'current_pr_authors': ['jun'],
            'current_head_reviewers': ['haru'],
            'excluded_assignees': [],
            'unknown_identity_assignees': [],
        })
        identities = {
            'aoi@shizuha.com': 'aoi',
            'kei@shizuha.com': 'kei',
            'ryo@shizuha.com': 'ryo',
            'haru@shizuha.com': 'haru',
        }
        captured = {}

        def resolve(team, **kwargs):
            captured['hard_exclude_emails'] = kwargs['hard_exclude_emails']
            return self.project.id, 'aoi@shizuha.com'

        with mock.patch.object(
            WorkflowService,
            '_linked_origin_pr_authors',
            return_value=({'jun'}, [], 1),
        ), mock.patch.object(
            WorkflowService,
            '_linked_pr_current_head_reviewers',
            return_value=(set(), ['forgejo_review_lookup_failed:ConnectionError'], 1),
        ), mock.patch.object(
            WorkflowService,
            '_agent_github_identity',
            side_effect=lambda email: identities.get((email or '').lower()),
        ), mock.patch.object(
            WorkflowService,
            '_is_team_member',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_is_agent_available_for_assignment',
            return_value=True,
        ), mock.patch.object(
            WorkflowService,
            '_resolve_team_assignee',
            side_effect=resolve,
        ):
            WorkflowService._enforce_status_lane_owner(item, item.status, [])

        self.assertEqual(captured['hard_exclude_emails'], {'haru@shizuha.com'})
