"""PLAT-5343 slice 1 — Pulse v2 candidate/roster snapshot (HLD v12 §4.2).

Pins the fail-closed trust boundary and the snapshot semantics of
``POST /api/internal/v2/project-membership-candidates:resolve``:

Negative-authentication matrix rows for the Admin→Pulse hop (Security
condition S2, akira 2026-09-09 — every §4.1 rejection clause carries an
explicit test assertion; each row asserts a typed 401, never ``200 + []``):

 1. tokenless request → ``missing_headers``
 2. generic tenant/user bearer token in place of the service credential →
    ``missing_headers`` (no service credential/assertion headers at all)
 3. wrong-service credential (the ID-issued credential on this hop) →
    ``unknown_caller``
 4. wrong audience → ``wrong_audience``
 5. wrong scope → ``wrong_scope``
 6. expired assertion → ``expired``
 7. future ``iat`` → ``future_iat``
 8. bad signature / algorithm violation (``alg: none``, missing ``alg``,
    HS/RS confusion, wrong key, tampered payload) → ``algorithm_not_allowed``
    / ``bad_signature``
 9. non-integer, negative, or zero ``sub`` → ``invalid_subject``
10. header ↔ credential mismatch (a configured credential presented under a
    different service name) → ``credential_mismatch``
11. confused-deputy: identity fields in the request body never flow into the
    resolution → ``caller_supplied_identity``

Plus the S3 semantics (stated clock skew, single-use jti denylist with
exp-bound retention) and the snapshot contract: candidate identity parity,
roster-evidence parity, ≤3 local queries, zero outbound calls, and the exact
response schema with ``valid_for_ms=500``.
"""
import base64
import json
import time
import uuid
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from tasks.models import Project, ProjectMembership, TeamRoster
from tasks.services.subject_assertion import subject_fingerprint

SCOPE = 'project-membership-candidates:resolve'
ISSUER = 'shizuha-admin'
AUDIENCE = 'shizuha-pulse'
CREDENTIAL = 'v2-hop-credential-admin'


def _generate_signing_key():
    """Fresh Ed25519 keypair; returns (private_key, public_pem)."""
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key, pub.decode('ascii')


def _sign_assertion(signing_key, *, issuer=ISSUER, audience=AUDIENCE, scope=SCOPE,
                    sub=4242, email='zen-plat8136@shizuha.com', iat=None, exp=None,
                    lifetime=5, jti=None, parent_hash=None, alg='EdDSA',
                    claims_override=None):
    now = int(time.time())
    iat = now if iat is None else iat
    exp = iat + lifetime if exp is None else exp
    claims = {
        'iss': issuer,
        'aud': audience,
        'scope': scope,
        'sub': str(sub),
        'iat': iat,
        'exp': exp,
        'jti': jti or str(uuid.uuid4()),
    }
    if email is not None:
        claims['email'] = email
    if parent_hash is not None:
        claims['parent_assertion_sha256'] = parent_hash
    if claims_override:
        claims.update(claims_override)
    if alg == 'none':
        return jwt.encode(claims, key='', algorithm='none')
    if alg == 'HS256':
        return jwt.encode(claims, key='shared-secret-confusion', algorithm='HS256')
    pem = signing_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(claims, pem, algorithm='EdDSA')


def _forged_token_with_header_alg(header_alg):
    """Hand-rolled token whose header claims an arbitrary algorithm."""
    header = base64.urlsafe_b64encode(
        json.dumps({'alg': header_alg, 'typ': 'JWT'}).encode()).rstrip(b'=').decode()
    payload = base64.urlsafe_b64encode(json.dumps({
        'iss': ISSUER, 'aud': AUDIENCE, 'scope': SCOPE, 'sub': '4242',
        'iat': int(time.time()), 'exp': int(time.time()) + 5,
        'jti': str(uuid.uuid4()), 'parent_assertion_sha256': 'a' * 64,
    }).encode()).rstrip(b'=').decode()
    return f'{header}.{payload}.Zm9yZ2VkLXNpZw'


V2_SETTINGS = override_settings(
    PULSE_V2_INTERNAL_SERVICE_CREDENTIALS={'shizuha-admin': CREDENTIAL},
)


class _V2SnapshotTestCase(APITestCase):
    """Shared fixtures: signing key, settings override, URL, auth headers."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.signing_key, cls.public_pem = _generate_signing_key()

    # Frozen process clock for every test below (aoi, PLAT-8136 flake fix —
    # PLS-1421 premerge specimen run 58134 @ ea669a06). The §4.1 skew clause
    # grants only 2s of real budget between token mint and verify; under a
    # contended parallel runner that budget can elapse before the POST lands
    # and an ``expired``/``future_iat`` flap fails a semantics test. The
    # tolerance is a §4.1 contract — never a test knob — so the cure is a
    # frozen clock, not a widened tolerance: mint (_sign_assertion) and
    # verify (subject_assertion now) observe the same T and every clause is
    # deterministic by construction. Value is arbitrary; only differences
    # matter and those live in the iat/exp offsets.
    _FROZEN_T = 1800000000.0

    def setUp(self):
        super().setUp()
        clock = patch('time.time', return_value=self._FROZEN_T)
        clock.start()
        self.addCleanup(clock.stop)
        self.url = reverse('internal-v2-project-membership-candidates-resolve')
        self._settings = V2_SETTINGS
        self._settings.enable()
        self.addCleanup(self._settings.disable)
        self.key_override = override_settings(
            PULSE_V2_ASSERTION_SIGNING_PUBLIC_KEYS=[self.public_pem],
        )
        self.key_override.enable()
        self.addCleanup(self.key_override.disable)

    def _auth_headers(self, assertion, service='shizuha-admin', credential=CREDENTIAL):
        headers = {}
        if service is not None:
            headers['HTTP_X_INTERNAL_SERVICE'] = service
        if credential is not None:
            headers['HTTP_X_INTERNAL_SERVICE_TOKEN'] = credential
        if assertion is not None:
            headers['HTTP_X_SHIZUHA_SUBJECT_ASSERTION'] = assertion
        return headers

    def _post(self, assertion=None, body=None, **header_kwargs):
        return self.client.post(
            self.url, data=body, format='json',
            **self._auth_headers(assertion, **header_kwargs),
        )

    def _valid_assertion(self, **kwargs):
        parent = kwargs.pop('parent_hash', 'a' * 64)
        return _sign_assertion(self.signing_key, parent_hash=parent, **kwargs)


class V2NegativeAuthenticationMatrixTests(_V2SnapshotTestCase):
    """One test per §4.1 rejection clause (S2 matrix, Admin→Pulse hop)."""

    def test_01_tokenless_request_rejected(self):
        response = self._post(assertion=None, service=None, credential=None)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'missing_headers')

    def test_02_tenant_bearer_token_is_not_service_proof(self):
        """A user/tenant JWT is identity for the public API, never service auth."""
        tenant_jwt = _sign_assertion(self.signing_key, issuer='shizuha-id',
                                     audience='shizuha-api', scope='openid')
        response = self.client.post(
            self.url,
            format='json',
            HTTP_AUTHORIZATION=f'Bearer {tenant_jwt}',
        )
        # Typed non-200, never a success-shaped snapshot (the exact reason key
        # is owned by whichever layer rejects first — DRF auth or the view).
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('candidates', response.json())

    def test_03_wrong_service_credential_rejected(self):
        """The ID-issued credential (unknown principal) on this hop → reject."""
        assertion = self._valid_assertion()
        response = self._post(assertion, service='shizuha-id', credential='id-credential')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'unknown_caller')

    def test_04_wrong_audience_rejected(self):
        assertion = self._valid_assertion(audience='shizuha-admin')
        response = self._post(assertion)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'wrong_audience')

    def test_05_wrong_scope_rejected(self):
        assertion = self._valid_assertion(scope='membership-projection:resolve')
        response = self._post(assertion)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'wrong_scope')

    def test_06_expired_assertion_rejected(self):
        now = int(time.time())
        # Lifetime 1s (within the 5s cap), expired 5s ago (beyond the 2s skew).
        assertion = self._valid_assertion(iat=now - 6, exp=now - 5)
        response = self._post(assertion)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'expired')

    def test_07_future_iat_rejected(self):
        now = int(time.time())
        assertion = self._valid_assertion(iat=now + 60, exp=now + 65)
        response = self._post(assertion)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'future_iat')

    def test_08a_alg_none_rejected(self):
        assertion = self._valid_assertion(alg='none')
        response = self._post(assertion)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'algorithm_not_allowed')

    def test_08b_hs256_confusion_rejected(self):
        """HS256 object where the asymmetric verification key is configured."""
        assertion = self._valid_assertion(alg='HS256')
        response = self._post(assertion)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'algorithm_not_allowed')

    def test_08c_rs256_confusion_rejected(self):
        forged = _forged_token_with_header_alg('RS256')
        response = self._post(forged)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'algorithm_not_allowed')

    def test_08d_wrong_key_signature_rejected(self):
        other_key, other_pem = _generate_signing_key()
        assertion = self._valid_assertion()
        with override_settings(PULSE_V2_ASSERTION_SIGNING_PUBLIC_KEYS=[other_pem]):
            response = self._post(assertion)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'bad_signature')

    def test_08e_tampered_payload_rejected(self):
        assertion = self._valid_assertion(sub=4242)
        header_b64, payload_b64, sig_b64 = assertion.split('.')
        tampered = payload_b64[:-2] + ('AA' if payload_b64[-2:] != 'AA' else 'BB')
        response = self._post(f'{header_b64}.{tampered}.{sig_b64}')
        self.assertEqual(response.status_code, 401)
        self.assertIn(response.json()['reason'],
                      ('bad_signature', 'assertion_malformed'))

    def test_09_invalid_subjects_rejected_before_lookup(self):
        for bad_sub in (0, -1, 'abc', None, 1.5, True):
            with self.subTest(sub=bad_sub):
                assertion = self._valid_assertion(sub=bad_sub)
                response = self._post(assertion)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()['reason'], 'invalid_subject')

    def test_10_header_credential_mismatch_rejected(self):
        """S4: a configured credential presented under another service name."""
        assertion = self._valid_assertion()
        two_credentials = override_settings(
            PULSE_V2_INTERNAL_SERVICE_CREDENTIALS={
                'shizuha-admin': CREDENTIAL,
                'shizuha-id': 'v2-hop-credential-id',
            },
        )
        with two_credentials:
            # shizuha-id IS a known caller here, but the presented credential
            # is bound to shizuha-admin — the binding rule rejects it.
            response = self._post(assertion, service='shizuha-id')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'credential_mismatch')

    def test_11_confused_deputy_body_identity_rejected(self):
        """sub/email in the request body can never flow into the resolution."""
        for body in (
            {'sub': 9999},
            {'email': 'attacker@shizuha.com'},
            {'user_id': 9999, 'aliases': ['attacker@shizuha.com']},
        ):
            with self.subTest(body=body):
                assertion = self._valid_assertion()
                response = self._post(assertion, body=body)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()['reason'], 'caller_supplied_identity')

    def test_missing_parent_assertion_hash_rejected(self):
        assertion = self._valid_assertion(parent_hash=None)
        response = self._post(assertion)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'missing_parent_assertion_hash')

    def test_replayed_jti_rejected(self):
        jti = str(uuid.uuid4())
        first = self._post(self._valid_assertion(jti=jti))
        self.assertEqual(first.status_code, 200)
        replay = self._post(self._valid_assertion(jti=jti))
        self.assertEqual(replay.status_code, 401)
        self.assertEqual(replay.json()['reason'], 'replayed_jti')

    def test_lifetime_exceeded_rejected(self):
        assertion = self._valid_assertion(lifetime=30)
        response = self._post(assertion)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['reason'], 'lifetime_exceeded')

    def test_stated_clock_skew_tolerated(self):
        """S3: the stated 2s skew absorbs modest iat/exp drift, no auth flap."""
        now = int(time.time())
        within_skew = self._post(self._valid_assertion(iat=now + 1, exp=now + 6))
        self.assertEqual(within_skew.status_code, 200)
        # exp=now stays inside the stated 2s skew if validation crosses the
        # next wall second. exp=now-1 rejects in that window (hiro, run 8660).
        now = int(time.time())
        slightly_past = self._post(self._valid_assertion(
            iat=now - 5, exp=now))
        self.assertEqual(
            slightly_past.status_code, 200,
            msg=f'skew-tolerance flap, reason body: '
                f'{getattr(slightly_past, "content", b"")[:400]!r}',
        )

    def test_unconfigured_route_fails_closed(self):
        """No credential/key provisioned → nobody is served (never tokenless)."""
        with override_settings(PULSE_V2_INTERNAL_SERVICE_CREDENTIALS={},
                               PULSE_V2_ASSERTION_SIGNING_PUBLIC_KEYS=[]):
            assertion = self._valid_assertion()
            response = self._post(assertion)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()['reason'], 'not_configured')


class V2SnapshotSemanticsTests(_V2SnapshotTestCase):
    """Candidate identity parity + roster evidence parity (HLD §4.2)."""

    def setUp(self):
        super().setUp()
        self.project_a = Project.objects.create(
            name='Alpha', key='ALPHA', org_id=1, access_level='private', is_active=True)
        self.project_b = Project.objects.create(
            name='Beta', key='BETA', org_id=2, access_level='private', is_active=True)

    def _resolve(self, sub=4242, email='zen-plat8136@shizuha.com'):
        response = self._post(self._valid_assertion(sub=sub, email=email))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_happy_path_schema(self):
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='member', is_active=True)
        snapshot = self._resolve()
        self.assertEqual(snapshot['schema'], 'shizuha.pulse.project-membership-candidates.v2')
        self.assertEqual(snapshot['valid_for_ms'], 500)
        self.assertTrue(snapshot['snapshot_id'])
        self.assertTrue(snapshot['generated_at'].endswith('Z'))
        self.assertEqual(
            snapshot['subject_fingerprint'],
            subject_fingerprint(ISSUER, 4242, 'zen-plat8136@shizuha.com'),
        )
        self.assertEqual(snapshot['candidates'], [
            {'project_id': self.project_a.id, 'organization_id': 1, 'role': 'member'},
        ])
        self.assertEqual(snapshot['roster_org_evidence'], [])

    def test_exact_sub_matches_non_null_user_id_row(self):
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, user_email='other-plat8136@shizuha.com',
            role='member', is_active=True)
        snapshot = self._resolve(sub=4242, email='zen-plat8136@shizuha.com')
        self.assertEqual(len(snapshot['candidates']), 1)
        self.assertEqual(snapshot['candidates'][0]['project_id'], self.project_a.id)

    def test_different_sub_never_matches_non_null_row(self):
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='member', is_active=True)
        snapshot = self._resolve(sub=777, email='zen-plat8136@shizuha.com')
        self.assertEqual(snapshot['candidates'], [])

    def test_alias_matches_only_null_user_id_rows(self):
        """Email aliases match only active rows whose user_id IS NULL."""
        ProjectMembership.objects.create(
            project=self.project_a, user_id=None, user_email='zen-plat8136@agents.shizuha.io',
            role='member', is_active=True)
        # Same alias on a row WITH a non-null different user_id must NOT match.
        ProjectMembership.objects.create(
            project=self.project_b, user_id=9999, user_email='zen-plat8136@agents.shizuha.io',
            role='member', is_active=True)
        snapshot = self._resolve(sub=4242, email='zen-plat8136@shizuha.com')
        project_ids = [c['project_id'] for c in snapshot['candidates']]
        self.assertEqual(project_ids, [self.project_a.id])

    def test_conflicting_non_null_user_id_email_row_denied(self):
        """The ONLY permitted deliberate difference vs v1 (HLD §4.4 step 2):
        a row with a non-null user_id conflicting with sub never matches merely
        because its email matches."""
        ProjectMembership.objects.create(
            project=self.project_a, user_id=9999, user_email='zen-plat8136@shizuha.com',
            role='member', is_active=True)
        snapshot = self._resolve(sub=4242, email='zen-plat8136@shizuha.com')
        self.assertEqual(snapshot['candidates'], [])

    def test_inactive_memberships_never_match(self):
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='member', is_active=False)
        ProjectMembership.objects.create(
            project=self.project_b, user_id=None, user_email='zen-plat8136@shizuha.com',
            role='member', is_active=False)
        snapshot = self._resolve()
        self.assertEqual(snapshot['candidates'], [])

    def test_role_dedup_owner_over_member_over_viewer(self):
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='viewer', is_active=True)
        ProjectMembership.objects.create(
            project=self.project_a, user_id=None, user_email='zen-plat8136@shizuha.com',
            role='owner', is_active=True)
        ProjectMembership.objects.create(
            project=self.project_b, user_id=4242, role='member', is_active=True)
        ProjectMembership.objects.create(
            project=self.project_b, user_id=None, user_email='zen-plat8136@agents.shizuha.io',
            role='viewer', is_active=True)
        snapshot = self._resolve()
        roles = {c['project_id']: c['role'] for c in snapshot['candidates']}
        self.assertEqual(roles, {self.project_a.id: 'owner', self.project_b.id: 'member'})

    def test_roster_evidence_exact_current_predicate(self):
        """Active TeamRoster, exact non-null org, alias present in tiers."""
        TeamRoster.objects.create(
            team_name='pod', organization_id=1, organization_slug='shizuha',
            tiers=[['zen-plat8136@shizuha.com']], is_active=True, version=12)
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='member', is_active=True)
        snapshot = self._resolve()
        roster = TeamRoster.objects.get(team_name='pod')
        self.assertEqual(snapshot['roster_org_evidence'], [
            {'organization_id': 1,
             'roster_rows': [{'id': roster.id, 'version': roster.version}]},
        ])

    def test_roster_evidence_excludes_inactive_null_org_foreign_org(self):
        TeamRoster.objects.create(
            team_name='inactive', organization_id=1, organization_slug='shizuha',
            tiers=[['zen-plat8136@shizuha.com']], is_active=False, version=1)
        TeamRoster.objects.create(
            team_name='null-org', organization_id=None, organization_slug='',
            tiers=[['zen-plat8136@shizuha.com']], is_active=True, version=2)
        TeamRoster.objects.create(
            team_name='foreign', organization_id=2, organization_slug='other',
            tiers=[['zen-plat8136@shizuha.com']], is_active=True, version=3)
        TeamRoster.objects.create(
            team_name='junior-only', organization_id=1, organization_slug='shizuha',
            tiers=[['someone-else-plat8136@shizuha.com']],
            junior_emails=['zen-plat8136@shizuha.com'], is_active=True, version=4)
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='member', is_active=True)
        snapshot = self._resolve()
        self.assertEqual(snapshot['roster_org_evidence'], [])

    def test_roster_evidence_only_for_candidate_orgs(self):
        """Roster evidence is scoped to the candidate organizations (the exact
        composition input); orgs without candidates contribute nothing."""
        TeamRoster.objects.create(
            team_name='org2', organization_id=2, organization_slug='other',
            tiers=[['zen-plat8136@shizuha.com']], is_active=True, version=5)
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='member', is_active=True)
        snapshot = self._resolve()
        self.assertEqual(snapshot['roster_org_evidence'], [])

    def test_fingerprint_stable_and_pii_free(self):
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='member', is_active=True)
        snapshot = self._resolve()
        # The asserted email (subject PII) never appears in the response.
        self.assertNotIn('zen-plat8136@shizuha.com', json.dumps(snapshot))
        # The fingerprint is stable across calls for the same subject.
        again = self._resolve()
        self.assertEqual(snapshot['subject_fingerprint'],
                         again['subject_fingerprint'])

    def test_authoritative_empty_is_valid_200(self):
        snapshot = self._resolve(sub=4242, email='nobody-plat8136@shizuha.com')
        self.assertEqual(snapshot['candidates'], [])
        self.assertEqual(snapshot['roster_org_evidence'], [])


class V2DependencyProofTests(_V2SnapshotTestCase):
    """§10 gate 3 (Pulse half): zero outbound calls, fixed query count."""

    def setUp(self):
        super().setUp()
        self.project_a = Project.objects.create(
            name='Alpha', key='ALPHA', org_id=1, access_level='private', is_active=True)
        self.project_b = Project.objects.create(
            name='Beta', key='BETA', org_id=2, access_level='private', is_active=True)
        TeamRoster.objects.create(
            team_name='pod', organization_id=1, organization_slug='shizuha',
            tiers=[['zen-plat8136@shizuha.com']], is_active=True, version=1)
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='member', is_active=True)
        ProjectMembership.objects.create(
            project=self.project_b, user_id=None, user_email='zen-plat8136@shizuha.com',
            role='viewer', is_active=True)

    def _forbid_outbound(self):
        """Any HTTP/RPC attempt during resolution fails the test loudly."""
        import requests as http_requests

        def boom(*args, **kwargs):
            raise AssertionError('Pulse v2 snapshot attempted an outbound HTTP call')

        return [
            patch.object(http_requests.Session, 'send', boom),
            patch.object(http_requests.Session, 'request', boom),
            patch.object(http_requests, 'request', boom),
            patch.object(http_requests, 'get', boom),
            patch.object(http_requests, 'post', boom),
        ]

    def test_zero_outbound_calls_and_bounded_queries(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        patches = self._forbid_outbound()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with CaptureQueriesContext(connection) as ctx:
                response = self._post(self._valid_assertion())
        self.assertEqual(response.status_code, 200)
        # HLD §4.2: at most three local RESOLUTION queries (here: candidates
        # + rosters). Counted per-table so transaction-fence statements
        # (BEGIN/SAVEPOINT under PostgreSQL) never pollute the gate.
        resolution = [
            q for q in ctx.captured_queries
            if 'tasks_projectmembership' in q['sql']
            or 'tasks_teamroster' in q['sql']
        ]
        self.assertLessEqual(len(resolution), 3, ctx.captured_queries)

    def test_empty_snapshot_uses_fewer_queries_still_bounded(self):
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        patches = self._forbid_outbound()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with CaptureQueriesContext(connection) as ctx:
                response = self._post(self._valid_assertion(sub=1, email='x@shizuha.com'))
        self.assertEqual(response.status_code, 200)
        resolution = [
            q for q in ctx.captured_queries
            if 'tasks_projectmembership' in q['sql']
            or 'tasks_teamroster' in q['sql']
        ]
        self.assertLessEqual(len(resolution), 3, ctx.captured_queries)


class V1V2ShadowParityMatrixTests(_V2SnapshotTestCase):
    """§4.4 steps 1-2 in CI: the full production-order matrix, v1 vs v2.

    For every matrix row the fixture defines one Admin-membership truth; the
    v1 projection is computed by the REAL v1 view (with the org-membership
    source stubbed to that truth) and the v2 projection by the REAL v2
    snapshot plus the Admin composition rule (org ∈ Admin set ∪ roster
    evidence). Zero unexpected grant/loss is asserted; the ONLY permitted
    deliberate difference is row 8's conflicting non-null ID/email denial.
    """

    def setUp(self):
        super().setUp()
        self.project_a = Project.objects.create(
            name='Alpha', key='ALPHA', org_id=1, access_level='private', is_active=True)
        self.project_b = Project.objects.create(
            name='Beta', key='BETA', org_id=2, access_level='private', is_active=True)

    def _v1_claim(self, user_id, email, admin_member_orgs):
        """Run the REAL v1 view with only the Admin seam stubbed.

        The stub replaces ``admin_service.is_org_member_strict`` — the external
        Admin service call at the v1 production boundary — so the view's REAL
        org-membership predicate (Admin truth OR active exact-org roster
        alias) executes end to end.
        """
        from unittest.mock import patch as mock_patch
        from tasks.views import InternalUserProjectMembershipsView
        from rest_framework.test import APIRequestFactory
        from tasks.services import admin_service as admin_service_module

        def fake_is_org_member_strict(uid, org_id, user_email=None):
            # Authoritative answer only: True (member) / False (not a member).
            # The REAL strict predicate then falls through to its roster
            # branch exactly as it does against the live Admin service.
            return org_id in admin_member_orgs

        factory = APIRequestFactory()
        request = factory.get(
            f'/api/internal/users/{user_id}/project-memberships/',
            {'email': email} if email else {})
        with mock_patch.object(admin_service_module.admin_service,
                               'is_org_member_strict',
                               side_effect=fake_is_org_member_strict):
            response = InternalUserProjectMembershipsView.as_view()(
                request, user_id=user_id)
        return response.data

    def _v2_claim(self, sub, email, admin_member_orgs):
        """Run the REAL v2 snapshot + the Admin composition rule."""
        from tasks.membership_v2 import build_membership_snapshot
        candidates, roster_evidence = build_membership_snapshot(sub, email)
        roster_orgs = {e['organization_id'] for e in roster_evidence}
        memberships = {}
        for candidate in candidates:
            org_id = candidate['organization_id']
            if org_id is None:
                continue
            if org_id in admin_member_orgs or org_id in roster_orgs:
                memberships[str(candidate['project_id'])] = candidate['role']
        return {
            'project_memberships': memberships,
            'project_ids': sorted(int(pid) for pid in memberships),
        }

    def _assert_parity(self, sub, email, admin_member_orgs,
                       expect_conflicting_row_denial=False):
        v1 = self._v1_claim(sub, email, admin_member_orgs)
        v2 = self._v2_claim(sub, email, admin_member_orgs)
        if expect_conflicting_row_denial:
            # The ONLY permitted deliberate difference: v2 denies the project
            # whose row has a conflicting non-null user_id.
            self.assertTrue(set(v2['project_ids']) <= set(v1['project_ids']),
                            f'v2 granted beyond v1: {v2} vs {v1}')
            self.assertLess(len(v2['project_ids']), len(v1['project_ids']),
                            'expected the conflicting-row denial difference')
        else:
            self.assertEqual(v1, v2, f'v1={v1} v2={v2}')
        return v1, v2

    def test_row_1_human_id_membership(self):
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='member', is_active=True)
        self._assert_parity(4242, 'zen-plat8136@shizuha.com', admin_member_orgs={1})

    def test_row_2_admin_email_only_row(self):
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='member', is_active=True)
        # Admin knows the subject only by an email-only membership row.
        self._assert_parity(4242, 'zen-plat8136@shizuha.com', admin_member_orgs=set())

    def test_row_3_projectmembership_email_only_alias(self):
        ProjectMembership.objects.create(
            project=self.project_a, user_id=None, user_email='zen-plat8136@agents.shizuha.io',
            role='member', is_active=True)
        self._assert_parity(4242, 'zen-plat8136@shizuha.com', admin_member_orgs={1})

    def test_row_4_roster_only_agent_no_admin_membership(self):
        TeamRoster.objects.create(
            team_name='pod', organization_id=1, organization_slug='shizuha',
            tiers=[['zen-plat8136@shizuha.com']], is_active=True, version=1)
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='viewer', is_active=True)
        self._assert_parity(4242, 'zen-plat8136@shizuha.com', admin_member_orgs=set())

    def test_row_5_inactive_rows_everywhere(self):
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='member', is_active=False)
        ProjectMembership.objects.create(
            project=self.project_b, user_id=None, user_email='zen-plat8136@shizuha.com',
            role='member', is_active=False)
        TeamRoster.objects.create(
            team_name='inactive', organization_id=1, organization_slug='shizuha',
            tiers=[['zen-plat8136@shizuha.com']], is_active=False, version=1)
        self._assert_parity(4242, 'zen-plat8136@shizuha.com', admin_member_orgs={1})

    def test_row_6_null_org_roster(self):
        TeamRoster.objects.create(
            team_name='null-org', organization_id=None, organization_slug='',
            tiers=[['zen-plat8136@shizuha.com']], is_active=True, version=1)
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='member', is_active=True)
        self._assert_parity(4242, 'zen-plat8136@shizuha.com', admin_member_orgs=set())

    def test_row_7_foreign_org_roster_and_project(self):
        TeamRoster.objects.create(
            team_name='foreign', organization_id=2, organization_slug='other',
            tiers=[['zen-plat8136@shizuha.com']], is_active=True, version=1)
        ProjectMembership.objects.create(
            project=self.project_b, user_id=4242, role='member', is_active=True)
        # Admin membership is in org 1 only; the roster is org 2 (foreign to
        # the Admin set but exact-org for the candidate) — v1's per-org check
        # accepts it, and v2's roster evidence must too.
        self._assert_parity(4242, 'zen-plat8136@shizuha.com', admin_member_orgs={1})

    def test_row_8_conflicting_non_null_id_email(self):
        # Both candidate orgs are Admin-authorized, so the ONLY v1/v2
        # difference is the conflicting row's denial.
        ProjectMembership.objects.create(
            project=self.project_a, user_id=9999, user_email='zen-plat8136@shizuha.com',
            role='member', is_active=True)
        ProjectMembership.objects.create(
            project=self.project_b, user_id=4242, role='member', is_active=True)
        v1, v2 = self._assert_parity(
            4242, 'zen-plat8136@shizuha.com', admin_member_orgs={1, 2},
            expect_conflicting_row_denial=True)
        # v1 unioned the conflicting row; v2 denies exactly that project.
        self.assertIn(str(self.project_a.id), v1['project_memberships'])
        self.assertNotIn(str(self.project_a.id), v2['project_memberships'])
        self.assertIn(str(self.project_b.id), v2['project_memberships'])

    def test_row_9_duplicate_roles(self):
        ProjectMembership.objects.create(
            project=self.project_a, user_id=4242, role='viewer', is_active=True)
        ProjectMembership.objects.create(
            project=self.project_a, user_id=None, user_email='zen-plat8136@shizuha.com',
            role='owner', is_active=True)
        self._assert_parity(4242, 'zen-plat8136@shizuha.com', admin_member_orgs={1})

    def test_row_10_known_authoritative_empty(self):
        self._assert_parity(4242, 'nobody-plat8136@shizuha.com', admin_member_orgs=set())
