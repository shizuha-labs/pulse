"""PLAT-1250 (EVOL-20 Ph1c): project-aware team routing in _resolve_team_assignee.

Behavior under test (HLD requirement 3, signed HLD ab8fc292):
- flag OFF (default): resolver pool is byte-for-byte unchanged;
- flag ON: members with an active AgentProjectMembership for the task's project
  are preferred — primary tier before secondary tier;
- no eligible affinity member (none for the project, all expired/inactive, or
  every member filtered by availability/cap upstream) → full-pool fallback, the
  resolver never idles a task on affinity;
- load/priority tie-breaks and unavailable/stopped-agent exclusion are preserved
  (affinity narrows the pool AFTER availability/cap and BEFORE the load min()).

Prerequisites: PLAT-1224 (read-side ordering, flag convention), PLAT-1225
(availability-aware matrix apply). Coordinated with PLS-208/PLS-203 resolver
safety work — this preference only ever *narrows* the already-safe pool.
"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from tasks.models import AgentProjectMembership, Project, TeamRoster
from tasks.services.workflow_service import WorkflowService


def _r(idx, uid, email, active=0, inflight=0):
    """A `resolved` row exactly as _resolve_team_assignee builds them."""
    return (idx, uid, email, active, inflight)


class PreferProjectAffinityUnitTests(TestCase):
    """_prefer_project_affinity over real AgentProjectMembership rows."""

    def setUp(self):
        self.project = Project.objects.create(
            name='Shizuha Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        self.other = Project.objects.create(
            name='Hive', key='HIVE', org_id=1,
            access_level='private', is_active=True,
        )
        self.pool = [
            _r(0, 101, 'nagi@shizuha.com', active=1),
            _r(1, 102, 'sara@shizuha.com', active=0),
            _r(2, 103, 'saki@shizuha.com', active=2),
        ]

    def _item(self, project=None):
        """A stand-in task carrying the fields affinity scopes on: the exact
        project_id (globally unique) and org_id. The resolver only passes `item`
        to lead-election paths (unused here); affinity reads project_id/org_id."""
        p = project or self.project
        return SimpleNamespace(project_id=p.id, org_id=p.org_id, project=p)

    def _mk(self, email, uid=None, tier='primary', project=None, active=True,
            expires_at=None):
        return AgentProjectMembership.objects.create(
            project=project or self.project,
            agent_user_email=email,
            agent_user_id=uid,
            tier=tier,
            active=active,
            expires_at=expires_at,
        )

    def test_flag_off_returns_pool_unchanged(self):
        self._mk('nagi@shizuha.com', 101, tier='primary')
        out = WorkflowService._prefer_project_affinity(list(self.pool), item=self._item())
        self.assertEqual(out, self.pool)

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_primary_members_preferred_over_non_members(self):
        self._mk('saki@shizuha.com', 103, tier='primary')
        out = WorkflowService._prefer_project_affinity(list(self.pool), item=self._item())
        self.assertEqual([r[2] for r in out], ['saki@shizuha.com'])

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_primary_tier_beats_secondary_tier(self):
        self._mk('nagi@shizuha.com', 101, tier='secondary')
        self._mk('sara@shizuha.com', 102, tier='primary')
        out = WorkflowService._prefer_project_affinity(list(self.pool), item=self._item())
        self.assertEqual([r[2] for r in out], ['sara@shizuha.com'])

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_secondary_used_when_no_primary_in_pool(self):
        self._mk('nagi@shizuha.com', 101, tier='secondary')
        out = WorkflowService._prefer_project_affinity(list(self.pool), item=self._item())
        self.assertEqual([r[2] for r in out], ['nagi@shizuha.com'])

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_no_membership_for_project_falls_back_to_full_pool(self):
        self._mk('nagi@shizuha.com', 101, tier='primary', project=self.other)
        out = WorkflowService._prefer_project_affinity(list(self.pool), item=self._item())
        self.assertEqual(out, self.pool)

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_inactive_and_expired_memberships_are_ignored(self):
        self._mk('nagi@shizuha.com', 101, tier='primary', active=False)
        self._mk('sara@shizuha.com', 102, tier='primary',
                 expires_at=timezone.now() - timedelta(hours=1))
        out = WorkflowService._prefer_project_affinity(list(self.pool), item=self._item())
        self.assertEqual(out, self.pool)

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_unexpired_expiry_still_counts(self):
        self._mk('sara@shizuha.com', 102, tier='primary',
                 expires_at=timezone.now() + timedelta(days=1))
        out = WorkflowService._prefer_project_affinity(list(self.pool), item=self._item())
        self.assertEqual([r[2] for r in out], ['sara@shizuha.com'])

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_email_only_membership_matches_pool_member(self):
        # pre-mirror rows may carry only the email (agent_user_id NULL)
        self._mk('SAKI@shizuha.com', None, tier='primary')
        out = WorkflowService._prefer_project_affinity(list(self.pool), item=self._item())
        self.assertEqual([r[2] for r in out], ['saki@shizuha.com'])

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_uid_membership_matches_when_emails_differ(self):
        # id-backed row with a stale/aliased email still matches by user_id
        self._mk('old-alias@shizuha.com', 102, tier='primary')
        out = WorkflowService._prefer_project_affinity(list(self.pool), item=self._item())
        self.assertEqual([r[2] for r in out], ['sara@shizuha.com'])

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_empty_inputs_are_passthrough(self):
        self._mk('sara@shizuha.com', 102, tier='primary')
        # empty resolved → [] regardless of scope
        self.assertEqual(
            WorkflowService._prefer_project_affinity([], item=self._item()), [])
        # no resolvable project scope (no item, no key) → pool unchanged
        self.assertEqual(
            WorkflowService._prefer_project_affinity(list(self.pool)), self.pool)

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_same_key_other_org_membership_does_not_leak(self):
        """Review P1: Project.key is unique only WITHIN an org. A membership on
        another org's project that happens to share the key must NOT influence
        routing for this org's project (cross-org misassignment / privacy leak)."""
        other_org_plat = Project.objects.create(
            name='Other-org PLAT', key='PLAT', org_id=2,
            access_level='private', is_active=True)
        # saki holds a primary membership on the OTHER org's same-key project only.
        self._mk('saki@shizuha.com', 103, tier='primary', project=other_org_plat)
        # Routing a task in THIS org's PLAT must ignore saki's cross-org row.
        out = WorkflowService._prefer_project_affinity(
            list(self.pool), item=self._item(self.project))
        self.assertEqual(out, self.pool)
        # Sanity: a membership on THIS org's PLAT is preferred.
        self._mk('nagi@shizuha.com', 101, tier='primary', project=self.project)
        out2 = WorkflowService._prefer_project_affinity(
            list(self.pool), item=self._item(self.project))
        self.assertEqual([r[2] for r in out2], ['nagi@shizuha.com'])

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_key_plus_org_fallback_scopes_by_org(self):
        """When no project_id is available, the key-only fallback must additionally
        match org_id — so a same-key project in another org is still excluded."""
        other_org_plat = Project.objects.create(
            name='Other-org PLAT', key='PLAT', org_id=2,
            access_level='private', is_active=True)
        self._mk('sara@shizuha.com', 102, tier='primary', project=self.project)   # org 1
        self._mk('saki@shizuha.com', 103, tier='primary', project=other_org_plat)  # org 2
        # item has no project_id but carries org_id=1 + key 'plat' → only sara.
        item = SimpleNamespace(project_id=None, org_id=1, project=None)
        out = WorkflowService._prefer_project_affinity(
            list(self.pool), item=item, project_key='plat')
        self.assertEqual([r[2] for r in out], ['sara@shizuha.com'])

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_bare_key_without_org_is_not_applied(self):
        """A key with no org cannot be disambiguated across orgs → skip affinity."""
        self._mk('sara@shizuha.com', 102, tier='primary')
        out = WorkflowService._prefer_project_affinity(
            list(self.pool), project_key='plat')  # no item → no org
        self.assertEqual(out, self.pool)

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_lookup_error_fails_open_to_full_pool(self):
        with patch(
            'tasks.models.AgentProjectMembership.objects'
        ) as mock_mgr:
            mock_mgr.filter.side_effect = RuntimeError('db down')
            out = WorkflowService._prefer_project_affinity(list(self.pool), item=self._item())
        self.assertEqual(out, self.pool)


class ResolveTeamAssigneeAffinityIntegrationTests(TestCase):
    """End-to-end through _resolve_team_assignee with a mocked roster/user layer:
    cross-project routes on ONE team whose members hold different memberships."""

    TEAM = 'engineering'
    ROSTER = ['nagi@shizuha.com', 'sara@shizuha.com', 'saki@shizuha.com']

    def setUp(self):
        self.plat = Project.objects.create(
            name='Shizuha Platform', key='PLAT', org_id=1,
            access_level='private', is_active=True,
        )
        self.hive = Project.objects.create(
            name='Hive', key='HIVE', org_id=1,
            access_level='private', is_active=True,
        )
        self.uid = {'nagi@shizuha.com': 101, 'sara@shizuha.com': 102, 'saki@shizuha.com': 103}
        TeamRoster.objects.update_or_create(
            organization_id=1,
            team_name=self.TEAM,
            defaults={
                'organization_slug': 'shizuha',
                'tiers': [list(self.ROSTER)],
                'is_active': True,
            },
        )
        # sara: PLAT primary; saki: PLAT secondary + HIVE primary; nagi: none
        AgentProjectMembership.objects.create(
            project=self.plat, agent_user_email='sara@shizuha.com',
            agent_user_id=102, tier='primary', active=True)
        AgentProjectMembership.objects.create(
            project=self.plat, agent_user_email='saki@shizuha.com',
            agent_user_id=103, tier='secondary', active=True)
        AgentProjectMembership.objects.create(
            project=self.hive, agent_user_email='saki@shizuha.com',
            agent_user_id=103, tier='primary', active=True)

    def _resolve(self, project_key, available=None, loads=None, exclude=None):
        """Run _resolve_team_assignee with the roster/availability/load layers
        patched; membership prefs come from the REAL DB rows above. Passes an
        `item` scoped to the project_key's project (org 1) so affinity resolves
        by the task's exact project_id, as production does."""
        available = available if available is not None else set(self.ROSTER)
        loads = loads or {}

        proj = Project.objects.filter(key__iexact=project_key, org_id=1).first()
        if proj is None:
            # e.g. 'scs' — a project with no memberships; affinity finds none.
            proj = Project.objects.create(
                name=project_key, key=project_key.upper(), org_id=1,
                access_level='private', is_active=True)
        item = SimpleNamespace(project_id=proj.id, org_id=proj.org_id, project=proj,
                               created_by_email=exclude)

        def _user(email):
            return {'id': self.uid[email.strip().lower()], 'email': email}

        with patch.object(WorkflowService, 'assignable_teams',
                          return_value={self.TEAM: list(self.ROSTER)}), \
             patch.object(WorkflowService, '_pod_roster',
                          return_value={self.TEAM: list(self.ROSTER)}), \
             patch.object(WorkflowService, '_junior_guarded_tiers',
                          side_effect=lambda team, tiers, org_slug='': tiers), \
             patch.object(WorkflowService, '_is_agent_available_for_assignment',
                          side_effect=lambda e: (e or '').strip().lower() in available), \
             patch.object(WorkflowService, '_project_stage_lead', return_value=None), \
             patch.object(WorkflowService, '_agent_active_load',
                          side_effect=lambda uid, email: loads.get(email, 0)), \
             patch.object(WorkflowService, '_agent_workable_load',
                          side_effect=lambda uid, email: loads.get(email, 0)), \
             patch('tasks.user_service.user_service.get_user_by_email',
                   side_effect=_user):
            return WorkflowService._resolve_team_assignee(
                self.TEAM, exclude_email=exclude, project_key=project_key, item=item)

    def test_flag_off_default_behavior_unchanged_least_loaded_wins(self):
        # nagi has no membership but the lowest load — flag off he still wins.
        _, email = self._resolve('plat', loads={'nagi@shizuha.com': 0,
                                                'sara@shizuha.com': 3,
                                                'saki@shizuha.com': 2})
        self.assertEqual(email, 'nagi@shizuha.com')

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_flag_on_primary_member_preferred_despite_higher_load(self):
        _, email = self._resolve('plat', loads={'nagi@shizuha.com': 0,
                                                'sara@shizuha.com': 3,
                                                'saki@shizuha.com': 2})
        self.assertEqual(email, 'sara@shizuha.com')

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_flag_on_secondary_used_when_primary_unavailable(self):
        # sara (primary) stopped → saki (secondary) preferred over nagi (none).
        _, email = self._resolve(
            'plat',
            available={'nagi@shizuha.com', 'saki@shizuha.com'},
            loads={'nagi@shizuha.com': 0, 'saki@shizuha.com': 4})
        self.assertEqual(email, 'saki@shizuha.com')

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_flag_on_never_assigns_unavailable_member(self):
        # ALL affinity members stopped → falls back to available non-member,
        # never to a stopped primary.
        _, email = self._resolve('plat', available={'nagi@shizuha.com'})
        self.assertEqual(email, 'nagi@shizuha.com')

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_flag_on_no_membership_project_falls_back_to_least_loaded(self):
        # No agent holds a membership for this project key → legacy behavior.
        _, email = self._resolve('scs', loads={'nagi@shizuha.com': 1,
                                               'sara@shizuha.com': 0,
                                               'saki@shizuha.com': 2})
        self.assertEqual(email, 'sara@shizuha.com')

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_flag_on_load_tiebreak_preserved_within_affinity_tier(self):
        # Same team, different project: HIVE routes to saki (its primary), and
        # within a tier the least-loaded still wins (single member here, but the
        # PLAT case above proves preference; this proves cross-project split).
        _, email = self._resolve('hive', loads={'nagi@shizuha.com': 0,
                                                'sara@shizuha.com': 0,
                                                'saki@shizuha.com': 4})
        self.assertEqual(email, 'saki@shizuha.com')

    @override_settings(PULSE_PROJECT_AFFINITY_RESOLVER_ENABLED=True)
    def test_flag_on_author_exclusion_still_applies_inside_affinity_tier(self):
        # sara is PLAT primary but authored the task → exclusion beats affinity;
        # saki (secondary) is the next affinity tier.
        _, email = self._resolve('plat', exclude='sara@shizuha.com')
        self.assertEqual(email, 'saki@shizuha.com')
