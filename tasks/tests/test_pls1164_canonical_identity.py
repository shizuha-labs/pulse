"""PLS-1164: canonical-identity resolution at the resolver layer.

The PLS-1154 audit rule: a roster email resolves ONLY if it equals a
principal's canonical ``User.email`` in shizuha-id. These tests pin the four
acceptance surfaces:

1. A roster row carrying the conventional ``@shizuha.com`` label for an
   agent principal (canonical shizuha-id email ``@agents.shizuha.io``)
   resolves for membership consumers — never silently unmatched.
2. Roster writes canonicalize member emails to the PLAT-4220 storage
   convention (``@shizuha.com`` labels for agent principals) with a migration
   signal, and REJECT phantoms (named reason class
   ``phantom_roster_identity``) — an unresolvable label is never silently
   stored. PLAT-4220 forbids ``*.shizuha.io`` roster labels, so storage
   canonicalization points at the label convention while RESOLUTION maps
   label -> principal truth.
3. Lane-health surfaces a per-seat ``phantom_identity`` flag.
4. shizuha-id (via UserService) is the only principal authority — resolution
   errors fail OPEN (never misread as phantoms).
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from tasks.models import Item, Project, TeamRoster
from tasks.services import team_roster_commands
from tasks import user_service  # noqa: F401 — import before mock.patch (pkgutil.resolve_name)
from tasks.services.canonical_identity import (
    _classify_cache,
    _roster_canon_cache,
    REASON_PHANTOM_ROSTER_IDENTITY,
    canonical_principal_email,
    canonicalize_roster_members,
    classify_roster_email,
    roster_member_canonical_map,
)


def _clear_identity_caches():
    """Module-level caches are process-global — isolate tests from each other."""
    _classify_cache.clear()
    _roster_canon_cache.clear()
from tasks.services.lane_health import _seat_verdict
from tasks.services.workflow_service import WorkflowService

User = get_user_model()

CANONICAL_AGENT = 'nagi@agents.shizuha.io'
VARIANT_LABEL = 'nagi@shizuha.com'
PHANTOM = 'ghost@shizuha.com'


def _principal(email, *args, **kwargs):
    """Fake shizuha-id: the agent principal answers both spellings."""
    norm = (email or '').strip().lower()
    if norm in (CANONICAL_AGENT, VARIANT_LABEL):
        return {'id': 141, 'email': CANONICAL_AGENT, 'username': 'nagi'}
    return None


class ClassifyRosterEmailTests(TestCase):
    def setUp(self):
        # The fake principal directory must not survive this test in either cache.
        self.addCleanup(_clear_identity_caches)
        _clear_identity_caches()

    def test_canonical_email_classifies_as_canonical(self):
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        side_effect=_principal):
            self.assertEqual(classify_roster_email(CANONICAL_AGENT), ('canonical', CANONICAL_AGENT))

    def test_variant_classifies_as_canonicalizable(self):
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        side_effect=_principal):
            status, canonical = classify_roster_email(VARIANT_LABEL)
        self.assertEqual(status, 'canonicalizable')
        self.assertEqual(canonical, CANONICAL_AGENT)

    def test_phantom_classifies_as_phantom(self):
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        return_value=None):
            status, canonical = classify_roster_email(PHANTOM)
        self.assertEqual(status, 'phantom')
        self.assertIsNone(canonical)

    def test_authority_outage_is_resolution_error_never_phantom(self):
        import requests
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        side_effect=requests.RequestException('id down')):
            status, _ = classify_roster_email(PHANTOM)
        self.assertEqual(status, 'resolution_error')

    def test_canonical_principal_email_returns_principal_email(self):
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        side_effect=_principal):
            self.assertEqual(canonical_principal_email(VARIANT_LABEL), CANONICAL_AGENT)
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        return_value=None):
            self.assertIsNone(canonical_principal_email(PHANTOM))


class CanonicalizeRosterMembersTests(TestCase):
    def setUp(self):
        # The fake principal directory must not survive this test in either cache.
        self.addCleanup(_clear_identity_caches)
        _clear_identity_caches()

    def test_variants_rewritten_structure_preserved_phantoms_collected(self):
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        side_effect=_principal):
            result = canonicalize_roster_members(
                [['lead@shizuha.com', CANONICAL_AGENT], ['other@shizuha.com']],
                ['jr@shizuha.com'],
            )
        # lead@/other@/jr@ are phantoms under the fake authority.
        self.assertEqual(result['phantoms'],
                         ['lead@shizuha.com', 'other@shizuha.com', 'jr@shizuha.com'])
        # The @agents.shizuha.io spelling canonicalizes to the PLAT-4220
        # storage label; a resolvable conventional label maps to itself
        # (no self-rewrite recorded).
        self.assertEqual(result['canonicalized'], {CANONICAL_AGENT: VARIANT_LABEL})
        self.assertEqual(result['tiers'][0][1], VARIANT_LABEL)
        self.assertEqual(result['resolution_errors'], [])

    def test_outage_fails_open_keeps_original_emails(self):
        import requests
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        side_effect=requests.RequestException('id down')):
            result = canonicalize_roster_members([[VARIANT_LABEL]], [])
        self.assertEqual(result['resolution_errors'], [VARIANT_LABEL])
        self.assertEqual(result['phantoms'], [])
        self.assertEqual(result['tiers'], [[VARIANT_LABEL]])


class IsTeamMemberCanonicalResolutionTests(TestCase):
    def setUp(self):
        # The fake principal directory must not survive this test in either cache.
        self.addCleanup(_clear_identity_caches)
        _clear_identity_caches()
        self.admin = User.objects.create_user(
            username='pls1164-admin', email='pls1164-admin@agents.shizuha.io',
            password='x', is_staff=True,
        )
        self.project = Project.objects.create(
            name='PLS1164', key='P164', org_id=1, access_level='open',
        )
        self.item = Item.objects.create(
            project=self.project,
            item_key='P164-1',
            title='probe',
            mode='task',
            status='todo',
            workflow_name='simple',
            workflow_status='todo',
            assignment_group='engineering',
            created_by_email=self.admin.email,
            created_by_id=self.admin.id,
        )
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_id=1, organization_slug='shizuha',
            defaults={
                'tiers': [[VARIANT_LABEL, 'kai@shizuha.com']],
                'junior_emails': [],
                'is_active': True,
                'updated_by': self.admin.email,
            },
        )

    def test_variant_roster_row_resolves_for_member_check(self):
        """Acceptance 1: the variant label resolves — never silently unmatched."""
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        side_effect=_principal):
            self.assertTrue(WorkflowService._is_team_member(
                CANONICAL_AGENT, 'engineering', item=self.item))
            # The variant spelling of the SAME principal is also a member.
            self.assertTrue(WorkflowService._is_team_member(
                VARIANT_LABEL, 'engineering', item=self.item))

    def test_phantom_member_does_not_match_and_is_observable(self):
        TeamRoster.objects.update_or_create(
            team_name='engineering', organization_id=1, organization_slug='shizuha',
            defaults={
                'tiers': [[PHANTOM]],
                'junior_emails': [],
                'is_active': True,
                'updated_by': self.admin.email,
            },
        )
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        return_value=None):
            self.assertFalse(WorkflowService._is_team_member(
                PHANTOM, 'engineering', item=self.item))
            cmap = roster_member_canonical_map('engineering', organization_id=1)
        self.assertIn(PHANTOM, cmap['phantoms'])
        self.assertEqual(cmap['errors'], [])

    def test_outage_fails_open_to_alias_verdict(self):
        """Acceptance 4: an identity outage never flips a member to non-member."""
        import requests
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        side_effect=requests.RequestException('id down')):
            # Alias swap still matches the domain variant of the same agent.
            self.assertTrue(WorkflowService._is_team_member(
                VARIANT_LABEL, 'engineering', item=self.item))


class RosterWriteBoundaryTests(TestCase):
    def setUp(self):
        # The fake principal directory must not survive this test in either cache.
        self.addCleanup(_clear_identity_caches)
        _clear_identity_caches()

    def test_command_add_variant_stores_canonical(self):
        """Acceptance 2: a non-canonical variant is canonicalized on write.

        The ``@agents.shizuha.io`` spelling of an agent principal is NOT a
        valid roster label (PLAT-4220 owned-domain rule); the write
        canonicalizes it to the conventional ``@shizuha.com`` label after
        verifying the principal resolves."""
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        side_effect=_principal):
            receipt = team_roster_commands.execute_team_roster_command(
                command_id='6f163004-6ee5-41e5-a964-09bad1e88e28',
                action='add', organization_id=1, organization_slug='shizuha',
                team_name='engineering', email=CANONICAL_AGENT,
                expected_version=0, actor_email='ops@agents.shizuha.io',
            )
        row = TeamRoster.objects.get(
            team_name='engineering', organization_id=1, organization_slug='shizuha',
        )
        members = TeamRoster._flat_member_emails(row.tiers, row.junior_emails)
        self.assertIn(VARIANT_LABEL, members)
        self.assertNotIn(CANONICAL_AGENT, members)
        self.assertEqual(receipt['action'], 'add')

    def test_command_add_phantom_rejected_with_named_reason(self):
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        return_value=None):
            with self.assertRaises(team_roster_commands.TeamRosterCommandError) as ctx:
                team_roster_commands.execute_team_roster_command(
                    command_id='6f163004-6ee5-41e5-a964-09bad1e88e29',
                    action='add', organization_id=1, organization_slug='shizuha',
                    team_name='engineering', email=PHANTOM,
                    expected_version=0, actor_email='ops@agents.shizuha.io',
                )
        self.assertEqual(ctx.exception.code, REASON_PHANTOM_ROSTER_IDENTITY)

    def test_command_add_outage_fails_open_stores_as_is(self):
        import requests
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        side_effect=requests.RequestException('id down')):
            team_roster_commands.execute_team_roster_command(
                command_id='6f163004-6ee5-41e5-a964-09bad1e88e28',
                action='add', organization_id=1, organization_slug='shizuha',
                team_name='engineering', email=PHANTOM,
                expected_version=0, actor_email='ops@agents.shizuha.io',
            )
        row = TeamRoster.objects.get(
            team_name='engineering', organization_id=1, organization_slug='shizuha',
        )
        self.assertIn(PHANTOM, TeamRoster._flat_member_emails(row.tiers, row.junior_emails))


class LaneHealthPhantomFlagTests(TestCase):
    def setUp(self):
        # The fake principal directory must not survive this test in either cache.
        self.addCleanup(_clear_identity_caches)
        _clear_identity_caches()

    def test_phantom_seat_flagged_not_silent(self):
        """Acceptance 3: lane-health shows the per-seat phantom-identity flag."""
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        return_value=None), \
             mock.patch('tasks.services.lane_health._seat_capacity', return_value={}), \
             mock.patch('tasks.services.lane_health._fleet_runtime_digest', return_value='x'), \
             mock.patch('tasks.services.workflow_service.WorkflowService._seat_routability_verdict',
                        side_effect=Exception('skip resolver')):
            verdict = _seat_verdict(PHANTOM, 'engineering', probe_item=None)
        self.assertTrue(verdict['phantom_identity'])
        self.assertIn('phantom_roster_identity', verdict['reasons'])

    def test_resolvable_seat_not_flagged(self):
        with mock.patch('tasks.user_service.UserService._get_id_user_by_email',
                        side_effect=_principal), \
             mock.patch('tasks.services.lane_health._seat_capacity', return_value={}), \
             mock.patch('tasks.services.lane_health._fleet_runtime_digest', return_value='x'), \
             mock.patch('tasks.services.workflow_service.WorkflowService._seat_routability_verdict',
                        side_effect=Exception('skip resolver')):
            verdict = _seat_verdict(CANONICAL_AGENT, 'engineering', probe_item=None)
        self.assertFalse(verdict['phantom_identity'])
        self.assertNotIn('phantom_roster_identity', verdict['reasons'])
