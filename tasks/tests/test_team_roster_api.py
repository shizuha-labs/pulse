from unittest.mock import patch
import uuid

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from tasks.models import Project, TeamRoster, TeamRosterCommandReceipt


def _ensure_roster(team_name, **fields):
    """Idempotently reconcile a TeamRoster row.

    Migrations seed the canonical teams (security/devops/engineering/review/
    research-analytics/…), so a bare ``TeamRoster.objects.create('devops', …)`` in a
    test setUp collides on ``tasks_team_roster_team_name_key``. These CRUD/API
    tests own the row they assert on, so update_or_create expresses their real
    intent — "ensure this team exists with these tiers" — without depending on
    whether a migration already seeded it.
    """
    obj, _ = TeamRoster.objects.update_or_create(team_name=team_name, defaults=fields)
    return obj


def _command(action, team_name, *, email='', organization_id=1,
             organization_slug='shizuha', command_id=None, expected_version=None):
    canonical = TeamRoster._CANONICAL_ALIASES.get(
        team_name.strip().lower().replace('_', '-').replace(' ', '-'),
        team_name.strip().lower().replace('_', '-').replace(' ', '-'),
    )
    if expected_version is None:
        row = TeamRoster.objects.filter(
            team_name=canonical,
            organization_id=organization_id,
            organization_slug=organization_slug,
        ).first()
        expected_version = row.version if row else 0
    return {
        'command_id': str(command_id or uuid.uuid4()),
        'action': action,
        'team_name': team_name,
        'organization_id': organization_id,
        'organization_slug': organization_slug,
        'email': email,
        'expected_version': expected_version,
    }


class TeamRosterMemberAPITests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='staff',
            email='staff@example.com',
            password='testpass',
            is_staff=True,
        )
        self.client.force_authenticate(self.user)

    def test_get_lists_membership_from_canonical_roster(self):
        _ensure_roster(
            'research-analytics',
            tiers=[['nao@shizuha.com'], ['banto@shizuha.com']],
            is_active=True,
        )

        resp = self.client.get('/api/admin/team-roster/members/', {'email': 'banto@shizuha.com'})

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['memberships'], [{
            'team_name': 'research-analytics',
            'tier_index': 1,
            'organization_id': 1,
            'organization_slug': 'shizuha',
        }])
        roster_names = {roster['team_name'] for roster in resp.data['rosters']}
        self.assertIn('research-analytics', roster_names)

    def test_get_rosters_carry_cas_version_for_command_writers(self):
        """PLS-745: writes are CAS commands on the roster version, and this GET
        is the only read Hive has — without ``version`` in the payload no client
        can construct a valid write (Hive team placement was broken fleet-wide
        until 2026-08-07 because of exactly that)."""
        row = _ensure_roster(
            'research-analytics',
            tiers=[['nao@shizuha.com']],
            is_active=True,
        )
        row.version = 9
        # TeamRoster.save() advances the optimistic command version on every
        # write, so read back what actually persisted instead of pinning 9.
        row.save(update_fields=['version'])
        row.refresh_from_db()
        self.assertGreaterEqual(row.version, 9)

        resp = self.client.get('/api/admin/team-roster/members/', {'email': ''})

        self.assertEqual(resp.status_code, 200, resp.data)
        # Team names are only unique per organization — key on both, or another
        # org's same-named roster (version 0) can clobber the row under test.
        matches = [
            r for r in resp.data['rosters']
            if r['team_name'] == 'research-analytics'
            and r['organization_id'] == row.organization_id
        ]
        self.assertEqual(len(matches), 1, resp.data['rosters'])
        self.assertEqual(matches[0]['version'], row.version)
        for roster in resp.data['rosters']:
            self.assertIsInstance(roster['version'], int)

    def test_project_scoped_get_allows_org_member_and_hides_foreign_rosters(self):
        self.user.is_staff = False
        self.user.organization_memberships = {4: 'owner'}
        project = Project.objects.create(
            name='Books', key='BKROSTER', org_id=4, access_level='private',
        )
        _ensure_roster(
            'accounting',
            organization_id=4,
            organization_slug='personal-3',
            tiers=[['sato@shizuha.com']],
            is_active=True,
        )
        _ensure_roster(
            'quant',
            organization_id=2,
            organization_slug='shizuha-digital',
            tiers=[['kane@shizuha.com']],
            is_active=True,
        )

        resp = self.client.get('/api/admin/team-roster/members/', {
            'email': 'sato@shizuha.com',
            'project_id': project.id,
        })

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['memberships'], [{
            'team_name': 'accounting',
            'tier_index': 0,
            'organization_id': 4,
            'organization_slug': 'personal-3',
        }])
        self.assertEqual(
            {(row['organization_id'], row['team_name']) for row in resp.data['rosters']},
            {(4, 'accounting'), (4, 'ceo-decisions')},
        )

    def test_project_scoped_get_rejects_foreign_project(self):
        self.user.is_staff = False
        self.user.organization_memberships = {4: 'owner'}
        foreign = Project.objects.create(
            name='Digital Books', key='BKFOREIGN', org_id=2,
            access_level='private',
        )

        resp = self.client.get('/api/admin/team-roster/members/', {
            'email': 'kane@shizuha.com',
            'project_id': foreign.id,
        })

        self.assertEqual(resp.status_code, 404, resp.data)

    def test_project_scoped_get_rejects_invalid_project_id(self):
        resp = self.client.get('/api/admin/team-roster/members/', {
            'email': 'sato@shizuha.com',
            'project_id': 'not-an-id',
        })

        self.assertEqual(resp.status_code, 400, resp.data)

    def test_unscoped_get_remains_staff_only(self):
        self.user.is_staff = False

        resp = self.client.get('/api/admin/team-roster/members/', {
            'email': 'sato@shizuha.com',
        })

        self.assertEqual(resp.status_code, 403, resp.data)

    def test_remove_deletes_email_from_all_tiers(self):
        roster = _ensure_roster(
            'research-analytics',
            tiers=[['nao@shizuha.com', 'banto@shizuha.com'], ['banto@shizuha.com']],
            junior_emails=['banto@shizuha.com'],
            is_active=True,
        )

        resp = self.client.post(
            '/api/admin/team-roster/members/',
            _command('remove', 'Analytics', email='banto@shizuha.com'),
            format='json',
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data['changed'])
        roster.refresh_from_db()
        self.assertEqual(roster.tiers, [['nao@shizuha.com'], []])
        self.assertEqual(roster.junior_emails, [])

    def test_add_creates_or_updates_canonical_roster(self):
        resp = self.client.post(
            '/api/admin/team-roster/members/',
            _command('add', 'Social Media', email='rin@shizuha.com'),
            format='json',
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        roster = TeamRoster.objects.get(team_name='social-media')
        self.assertEqual(roster.tiers, [['rin@shizuha.com']])

    def test_add_moves_member_from_previous_team(self):
        _ensure_roster(
            'customer-support',
            tiers=[['cora@shizuha.com', 'rin@shizuha.com']],
            is_active=True,
        )

        resp = self.client.post(
            '/api/admin/team-roster/members/',
            _command('add', 'Social Media', email='rin@shizuha.com'),
            format='json',
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(TeamRoster.objects.get(team_name='social-media').tiers, [['rin@shizuha.com']])
        self.assertEqual(TeamRoster.objects.get(team_name='customer-support').tiers, [['cora@shizuha.com']])

    def test_create_adds_empty_canonical_roster(self):
        # `program-management` is migration-seeded with tiers; this test asserts
        # a *fresh* create yields an empty roster, so remove any seeded row first.
        TeamRoster.objects.filter(team_name='program-management').delete()
        resp = self.client.post(
            '/api/admin/team-roster/members/',
            _command('create', 'Program Management'),
            format='json',
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        roster = TeamRoster.objects.get(team_name='program-management')
        self.assertEqual(roster.tiers, [])
        self.assertTrue(roster.is_active)

    def test_create_reactivates_existing_roster(self):
        _ensure_roster(
            'sales',
            tiers=[],
            is_active=False,
        )

        resp = self.client.post(
            '/api/admin/team-roster/members/',
            _command('create', 'Sales'),
            format='json',
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        roster = TeamRoster.objects.get(team_name='sales')
        self.assertTrue(roster.is_active)

    @patch(
        'tasks.services.admin_service.admin_service.get_organization_by_slug',
        return_value={'id': 460, 'slug': 'komal-soni', 'name': 'Komal Soni'},
    )
    def test_add_resolves_dynamic_organization_identity(self, _get_org):
        resp = self.client.post(
            '/api/admin/team-roster/members/',
            _command(
                'add', 'Accounting', email='sato@shizuha.com',
                organization_slug='komal-soni', organization_id=460,
            ),
            format='json',
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        roster = TeamRoster.objects.get(
            team_name='accounting',
            organization_slug='komal-soni',
        )
        self.assertEqual(roster.organization_id, 460)
        self.assertEqual(roster.tiers, [['sato@shizuha.com']])

    def test_model_save_enforces_single_active_team_membership(self):
        _ensure_roster(
            'review',
            tiers=[['reika@shizuha.com', 'kei@shizuha.com']],
            junior_emails=['reika@shizuha.com'],
            is_active=True,
        )

        _ensure_roster(
            'merge',
            tiers=[['kei@shizuha.com']],
            is_active=True,
        )

        review = TeamRoster.objects.get(team_name='review')
        self.assertEqual(review.tiers, [['reika@shizuha.com']])
        self.assertEqual(review.junior_emails, ['reika@shizuha.com'])

    def test_command_replay_is_payload_bound_and_does_not_bump_version_twice(self):
        command_id = uuid.uuid4()
        payload = _command(
            'add', 'Social Media', email='rin@shizuha.com',
            command_id=command_id,
        )

        first = self.client.post(
            '/api/admin/team-roster/members/', payload, format='json',
        )
        replay = self.client.post(
            '/api/admin/team-roster/members/', payload, format='json',
        )

        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertFalse(first.data['idempotent_replay'])
        self.assertTrue(replay.data['idempotent_replay'])
        self.assertEqual(replay.data['version'], first.data['version'])
        self.assertEqual(
            TeamRosterCommandReceipt.objects.filter(command_id=command_id).count(),
            1,
        )

        conflict = self.client.post(
            '/api/admin/team-roster/members/',
            {**payload, 'email': 'cora@shizuha.com'},
            format='json',
        )
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.assertEqual(conflict.data['error'], 'idempotency_payload_conflict')

    def test_stale_expected_version_rejects_without_mutation(self):
        roster = _ensure_roster(
            'social-media', tiers=[['cora@shizuha.com']], is_active=True,
        )
        stale = max(roster.version - 1, 0)

        resp = self.client.post(
            '/api/admin/team-roster/members/',
            _command(
                'add', 'Social Media', email='rin@shizuha.com',
                expected_version=stale,
            ),
            format='json',
        )

        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertEqual(resp.data['error'], 'expected_version_conflict')
        roster.refresh_from_db()
        self.assertEqual(roster.tiers, [['cora@shizuha.com']])

    def test_remove_absent_team_commits_authoritative_empty_receipt(self):
        TeamRoster.objects.filter(team_name='empty-authority').delete()

        resp = self.client.post(
            '/api/admin/team-roster/members/',
            _command('remove', 'Empty Authority', email='nobody@shizuha.com'),
            format='json',
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data['authoritative_empty'])
        self.assertEqual(resp.data['members'], [])
        self.assertTrue(TeamRoster.objects.get(
            team_name='empty-authority', organization_id=1,
        ).is_active)

    def test_command_requires_explicit_idempotency_and_version(self):
        resp = self.client.post('/api/admin/team-roster/members/', {
            'action': 'add', 'team_name': 'Social Media',
            'email': 'rin@shizuha.com',
        }, format='json')

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn(resp.data['error'], {'command_id_invalid', 'version_or_org_invalid'})


# HIVE-237: TeamRoster lifecycle CRUD (create/rename/delete/set-lead)
from rest_framework.test import APIClient  # noqa: E402

_BASE = '/api/admin/team-roster/'


def _url(team_name='', extra=''):
    if team_name:
        return f'{_BASE}{team_name}/{extra}'
    return _BASE


class TeamRosterLifecycleBase(APITestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username='lcadmin', email='lcadmin@shizuha.com',
            password='pw', is_staff=True,
        )
        self.nonstaff = get_user_model().objects.create_user(
            username='lcuser', email='lcuser@shizuha.com',
            password='pw', is_staff=False,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _make_team(self, name, tiers=None, **kwargs):
        return _ensure_roster(name, tiers=tiers or [], **kwargs)


class TeamRosterListTests(TeamRosterLifecycleBase):
    def test_list_returns_all_teams(self):
        self._make_team('engineering', tiers=[['a@x.com']])
        self._make_team('review', tiers=[['r@x.com']])
        resp = self.client.get(_BASE)
        self.assertEqual(resp.status_code, 200, resp.data)
        names = [t['team_name'] for t in resp.json()['teams']]
        self.assertIn('engineering', names)
        self.assertIn('review', names)

    def test_list_includes_inactive(self):
        self._make_team('old-team', is_active=False)
        resp = self.client.get(_BASE)
        names = [t['team_name'] for t in resp.json()['teams']]
        self.assertIn('old-team', names)

    def test_list_forbidden_for_nonstaff(self):
        self.client.force_authenticate(self.nonstaff)
        resp = self.client.get(_BASE)
        self.assertEqual(resp.status_code, 403)


class TeamRosterCreateTests(TeamRosterLifecycleBase):
    def test_create_empty_team(self):
        resp = self.client.post(_BASE, {'team_name': 'new-squad'}, format='json')
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.json()['team_name'], 'new-squad')
        self.assertEqual(resp.json()['tiers'], [])
        self.assertTrue(resp.json()['is_active'])
        self.assertTrue(TeamRoster.objects.filter(team_name='new-squad').exists())

    def test_create_with_tiers(self):
        resp = self.client.post(
            _BASE,
            {'team_name': 'alpha', 'tiers': [['a@x.com'], ['b@x.com']]},
            format='json',
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.json()['tiers'], [['a@x.com'], ['b@x.com']])

    def test_create_missing_name_returns_400(self):
        resp = self.client.post(_BASE, {}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_create_duplicate_returns_409(self):
        self._make_team('existing')
        resp = self.client.post(_BASE, {'team_name': 'existing'}, format='json')
        self.assertEqual(resp.status_code, 409)

    def test_create_forbidden_for_nonstaff(self):
        self.client.force_authenticate(self.nonstaff)
        resp = self.client.post(_BASE, {'team_name': 'x'}, format='json')
        self.assertEqual(resp.status_code, 403)

    @patch(
        'tasks.services.admin_service.admin_service.get_organization_by_slug',
        return_value={'id': 460, 'slug': 'komal-soni', 'name': 'Komal Soni'},
    )
    def test_create_resolves_dynamic_organization_identity(self, _get_org):
        resp = self.client.post(_BASE, {
            'team_name': 'accounting',
            'organization_slug': 'komal-soni',
            'tiers': [['sato@shizuha.com']],
        }, format='json')

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data['organization_id'], 460)
        roster = TeamRoster.objects.get(
            team_name='accounting',
            organization_slug='komal-soni',
        )
        self.assertEqual(roster.organization_id, 460)

    @patch(
        'tasks.services.admin_service.admin_service.get_organization_by_slug',
        return_value={'id': 460, 'slug': 'komal-soni', 'name': 'Komal Soni'},
    )
    def test_create_rejects_slug_id_mismatch(self, _get_org):
        resp = self.client.post(_BASE, {
            'team_name': 'accounting',
            'organization_slug': 'komal-soni',
            'organization_id': 461,
        }, format='json')

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(TeamRoster.objects.filter(
            team_name='accounting',
            organization_slug='komal-soni',
        ).exists())

    @patch(
        'tasks.services.admin_service.admin_service.get_organization_by_slug',
        return_value=None,
    )
    def test_create_rejects_unresolved_dynamic_organization(self, _get_org):
        resp = self.client.post(_BASE, {
            'team_name': 'accounting',
            'organization_slug': 'unknown-tenant',
        }, format='json')

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(TeamRoster.objects.filter(
            team_name='accounting',
            organization_slug='unknown-tenant',
        ).exists())


class TeamRosterDetailTests(TeamRosterLifecycleBase):
    def setUp(self):
        super().setUp()
        self.team = self._make_team(
            'devops',
            tiers=[['mika@shizuha.com', 'ichi@shizuha.com']],
        )

    def test_get_returns_team(self):
        resp = self.client.get(_url('devops'))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.json()['team_name'], 'devops')

    def test_get_not_found(self):
        resp = self.client.get(_url('nonexistent'))
        self.assertEqual(resp.status_code, 404)

    def test_rename_team(self):
        resp = self.client.patch(_url('devops'), {'team_name': 'platform'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.json()['team_name'], 'platform')
        self.assertFalse(TeamRoster.objects.filter(team_name='devops').exists())
        self.assertTrue(TeamRoster.objects.filter(team_name='platform').exists())

    def test_set_lead_places_at_tier0_position0(self):
        resp = self.client.patch(
            _url('devops'),
            {'lead_email': 'ichi@shizuha.com'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        tiers = resp.json()['tiers']
        self.assertEqual(tiers[0][0], 'ichi@shizuha.com')
        self.assertIn('mika@shizuha.com', tiers[0])

    def test_set_lead_on_empty_tiers_creates_tier0(self):
        self._make_team('empty-squad', tiers=[])
        resp = self.client.patch(
            _url('empty-squad'),
            {'lead_email': 'lead@x.com'},
            format='json',
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.json()['tiers'], [['lead@x.com']])

    def test_update_note(self):
        resp = self.client.patch(_url('devops'), {'note': 'infra team'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.json()['note'], 'infra team')

    def test_deactivate_team(self):
        resp = self.client.delete(_url('devops'))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(resp.json()['is_active'])
        self.team.refresh_from_db()
        self.assertFalse(self.team.is_active)

    def test_delete_not_found(self):
        resp = self.client.delete(_url('ghost'))
        self.assertEqual(resp.status_code, 404)

    def test_patch_forbidden_for_nonstaff(self):
        self.client.force_authenticate(self.nonstaff)
        resp = self.client.patch(_url('devops'), {'note': 'x'}, format='json')
        self.assertEqual(resp.status_code, 403)


class InternalTeamRosterMemberMutationTests(APITestCase):
    """PLAT-6071: service-to-service roster mutation endpoint.

    Admin validates + consumes the JIT grant, then forwards the exact mutation
    to POST /api/internal/team-roster/members/ (network-secured/AllowAny, same
    trust model as the read-only InternalTeamRosterView). This endpoint applies
    the CAS command and returns its durable receipt; it never validates or
    consumes a JIT grant itself.
    """

    def _post(self, payload, **headers):
        return self.client.post(
            '/api/internal/team-roster/members/',
            payload,
            format='json',
            **headers,
        )

    def test_mutates_roster_and_returns_receipt(self):
        _ensure_roster('devops', tiers=[], is_active=True)
        payload = _command('add', 'devops', email='nova@shizuha.com')
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['email'], 'nova@shizuha.com')
        self.assertEqual(resp.data['action'], 'add')
        self.assertIn('version', resp.data)
        row = TeamRoster.objects.get(team_name='devops')
        self.assertIn('nova@shizuha.com', {m for tier in (row.tiers or []) for m in tier})

    def test_idempotent_retry_replays_same_receipt(self):
        _ensure_roster('devops', tiers=[], is_active=True)
        payload = _command('add', 'devops', email='nova@shizuha.com')
        first = self._post(payload)
        self.assertEqual(first.status_code, 200, first.data)
        second = self._post(payload)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data['command_id'], first.data['command_id'])
        self.assertEqual(second.data['version'], first.data['version'])

    def test_stale_expected_version_409_without_mutation(self):
        _ensure_roster('devops', tiers=[['existing@shizuha.com']], is_active=True)
        payload = _command('add', 'devops', email='nova@shizuha.com', expected_version=0)
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertEqual(resp.data['error'], 'expected_version_conflict')
        # No partial mutation: the roster still holds only the existing member.
        row = TeamRoster.objects.get(team_name='devops')
        self.assertNotIn('nova@shizuha.com', {m for tier in (row.tiers or []) for m in tier})

    def test_invalid_action_400(self):
        payload = _command('explode', 'devops', email='nova@shizuha.com')
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data['error'], 'action_invalid')


class InternalTeamRosterVersionTests(APITestCase):
    """PLAT-6071: the internal roster read carries the CAS version so service
    consumers (Admin JIT forward) can construct a valid mutation command."""

    def test_internal_roster_carries_version(self):
        _ensure_roster('devops', tiers=[], is_active=True)
        resp = self.client.get('/api/internal/team-roster/')
        self.assertEqual(resp.status_code, 200, resp.data)
        row = next(
            r for r in resp.data['teams']
            if r['team_name'] == 'devops'
        )
        self.assertIsInstance(row['version'], int)
        self.assertEqual(row['version'], TeamRoster.objects.get(team_name='devops').version)
