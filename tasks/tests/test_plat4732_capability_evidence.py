"""PLAT-4732 — adversarial vectors for the Hive capability-evidence verifier.

Proves, against a locally-generated Ed25519 producer key (the real Hive private
key never leaves Hive), that ``verify_evidence_jws`` ACCEPTS a fully-formed
exact-seat ``HiveCapabilityEvidenceV2`` token and FAIL-CLOSES on every attack
class the frozen HLD v18 §5.2 contract enumerates: ``none``/alg-substitution,
unknown/foreign ``kid``, each wrong seat/request/policy/generation binding,
malformed or mis-shaped results, and stale / not-yet-valid / over-lifetime
tokens. A local test key is the sanctioned test seam; the golden SHA vector from
HIVE-1153 will additionally pin byte-exactness with the producer.
"""
import hashlib
import json
import os
import time
from unittest import mock

import jwt as _jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import SimpleTestCase, TestCase

from tasks.models import (
    Item, Project, RoutingDenialAudit, SubstrateShadowMetric,
)
from tasks.services import capability_evidence as ce
from tasks.services.workflow_service import WorkflowService


def _ed25519_keypair(kid):
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_jwk = json.loads(_jwt.algorithms.OKPAlgorithm.to_jwk(priv.public_key()))
    pub_jwk['kid'] = kid
    return priv_pem, pub_jwk


class CapabilityEvidenceVerifierTests(SimpleTestCase):
    KID = 'hive-cap-ed25519-2026-07'
    GEN_ID = 'gen-2026-07-17T20:55Z'
    GEN_DIGEST = 'sha256:deadbeef'

    def setUp(self):
        self.priv_pem, self.pub_jwk = _ed25519_keypair(self.KID)
        self.pinned_jwks = {self.KID: _jwt.algorithms.OKPAlgorithm.from_jwk(json.dumps(self.pub_jwk))}
        self.pinned_gen = {'producer_generation_id': self.GEN_ID,
                           'producer_generation_digest': self.GEN_DIGEST, 'kid': self.KID}
        self.email = 'mio@shizuha.com'
        self.org = 1
        self.team = 'devops'
        self.reqkey = 'autonomous-dev::in_progress'
        self.required = ['kubeconfig']
        self.request = ce.build_evidence_request(
            email=self.email, organization_id=self.org, team_slug=self.team,
            requirement_key=self.reqkey, required_capabilities=self.required,
            item_id=20626, assignment_generation=7, request_id='11111111-1111-1111-1111-111111111111')

    def _claims(self, **over):
        now = int(time.time())
        claims = {
            'schema_version': ce.EVIDENCE_SCHEMA, 'iss': ce.EVIDENCE_ISSUER,
            'aud': ce.EVIDENCE_AUDIENCE, 'sub': self.email, 'organization_id': self.org,
            'team_slug': self.team, 'request_id': self.request['request_id'],
            'request_sha256': ce.request_sha256(self.request), 'requirement_key': self.reqkey,
            'required_capabilities': ['kubeconfig'], 'results': {'kubeconfig': 'PRESENT'},
            'materialization_generation': 'mat-sha256:' + 'a' * 64,
            'producer_generation_id': self.GEN_ID,
            'producer_generation_digest': self.GEN_DIGEST,
            'iat': now, 'nbf': now, 'exp': now + 5, 'jti': 'jti-abc',
        }
        claims.update(over)
        return claims

    def _mint(self, claims=None, *, typ=None, kid=None, alg='EdDSA', key=None):
        headers = {'typ': ce.EVIDENCE_TYP if typ is None else typ,
                   'kid': self.KID if kid is None else kid}
        return _jwt.encode(self._claims() if claims is None else claims,
                           self.priv_pem if key is None else key,
                           algorithm=alg, headers=headers)

    def _verify(self, token):
        return ce.verify_evidence_jws(
            token, request=self.request, expected_email=self.email,
            expected_org_id=self.org, expected_team_slug=self.team,
            requirement_key=self.reqkey, required_capabilities=self.required,
            pinned_jwks=self.pinned_jwks, pinned_generation=self.pinned_gen)

    def _assert_unknown(self, token, reason):
        with self.assertRaises(ce.EvidenceAuthorityUnknown) as ctx:
            self._verify(token)
        self.assertEqual(ctx.exception.reason, reason)

    # --- happy path ---
    def test_valid_token_accepted_present_capability(self):
        ev = self._verify(self._mint())
        self.assertTrue(ev.satisfies(['kubeconfig']))
        self.assertEqual(ev.present_capabilities, frozenset({'kubeconfig'}))

    def test_valid_token_absent_capability_does_not_satisfy(self):
        ev = self._verify(self._mint(self._claims(results={'kubeconfig': 'ABSENT'})))
        self.assertFalse(ev.satisfies(['kubeconfig']))

    # --- algorithm / signature attacks ---
    def test_none_alg_rejected(self):
        tok = _jwt.encode(self._claims(), key=None, algorithm='none',
                          headers={'typ': ce.EVIDENCE_TYP, 'kid': self.KID})
        self._assert_unknown(tok, 'alg_not_allowed')

    def test_hs256_substitution_rejected(self):
        # Attacker signs HS256 with the (public) JWK material as the shared secret.
        tok = _jwt.encode(self._claims(), key='public-x-as-secret', algorithm='HS256',
                          headers={'typ': ce.EVIDENCE_TYP, 'kid': self.KID})
        self._assert_unknown(tok, 'alg_not_allowed')

    def test_foreign_key_same_kid_rejected_bad_signature(self):
        other_pem, _ = _ed25519_keypair(self.KID)
        self._assert_unknown(self._mint(key=other_pem), 'bad_signature')

    # --- header attacks ---
    def test_wrong_typ_rejected(self):
        self._assert_unknown(self._mint(typ='JWT'), 'typ_mismatch')

    def test_unknown_kid_rejected(self):
        # kid matches the pinned generation but is absent from the JWK set.
        gen = dict(self.pinned_gen, kid='rotated-out')
        with self.assertRaises(ce.EvidenceAuthorityUnknown) as ctx:
            ce.verify_evidence_jws(
                self._mint(kid='rotated-out'), request=self.request,
                expected_email=self.email, expected_org_id=self.org,
                expected_team_slug=self.team, requirement_key=self.reqkey,
                required_capabilities=self.required, pinned_jwks=self.pinned_jwks,
                pinned_generation=gen)
        self.assertEqual(ctx.exception.reason, 'unknown_kid')

    def test_kid_not_current_generation_rejected(self):
        self._assert_unknown(self._mint(kid='some-other-kid'), 'kid_not_current_generation')

    def test_duplicate_pinned_kids_rejected(self):
        env = {
            'PULSE_HIVE_EVIDENCE_PUBLIC_JWKS': json.dumps({
                'keys': [self.pub_jwk, dict(self.pub_jwk)],
            }),
        }
        with self.assertRaises(ce.EvidenceAuthorityUnknown) as ctx:
            ce._load_pinned_jwks(env)
        self.assertEqual(ctx.exception.reason, 'duplicate_pinned_kid')

    # --- claim / binding attacks ---
    def test_wrong_subject_rejected(self):
        self._assert_unknown(self._mint(self._claims(sub='kane@shizuha.com')), 'wrong_subject')

    def test_wrong_org_rejected(self):
        self._assert_unknown(self._mint(self._claims(organization_id=3)), 'wrong_org')

    def test_wrong_team_rejected(self):
        self._assert_unknown(self._mint(self._claims(team_slug='review')), 'wrong_team')

    def test_wrong_issuer_rejected(self):
        self._assert_unknown(self._mint(self._claims(iss='evil')), 'invalid_token')

    def test_wrong_audience_rejected(self):
        self._assert_unknown(self._mint(self._claims(aud='shizuha-hive')), 'invalid_token')

    def test_request_sha_mismatch_rejected(self):
        self._assert_unknown(self._mint(self._claims(request_sha256='sha256:0000')), 'wrong_request_sha256')

    def test_requirement_key_mismatch_rejected(self):
        self._assert_unknown(self._mint(self._claims(requirement_key='other::stage')), 'wrong_requirement_key')

    def test_producer_generation_not_current_rejected(self):
        self._assert_unknown(
            self._mint(self._claims(producer_generation_id='gen-old')),
            'producer_generation_not_current')

    def test_required_capabilities_mismatch_rejected(self):
        self._assert_unknown(
            self._mint(self._claims(required_capabilities=['kubeconfig', 'host-exec'],
                                    results={'kubeconfig': 'PRESENT', 'host-exec': 'PRESENT'})),
            'required_capabilities_mismatch')

    def test_extra_signed_payload_claim_rejected(self):
        self._assert_unknown(
            self._mint(self._claims(debug_authority='smuggled')),
            'unexpected_claim_keys')

    def test_results_missing_requested_capability_rejected(self):
        self._assert_unknown(self._mint(self._claims(results={})), 'results_shape_mismatch')

    def test_results_extra_capability_rejected(self):
        self._assert_unknown(
            self._mint(self._claims(results={'kubeconfig': 'PRESENT', 'host-exec': 'PRESENT'})),
            'results_shape_mismatch')

    def test_malformed_result_value_rejected(self):
        self._assert_unknown(self._mint(self._claims(results={'kubeconfig': 'MAYBE'})), 'malformed_result')

    def test_missing_materialization_generation_rejected(self):
        self._assert_unknown(
            self._mint(self._claims(materialization_generation='')),
            'bad_materialization_generation')

    # HIVE-1153 P2 #1: the materialization digest is `mat-sha256:<64 hex>` — a
    # malformed value (wrong prefix, short digest, non-hex) is rejected, not just
    # emptiness. The producer emits `mat-{sha256:hex}` (hive capability_evidence).
    def test_malformed_materialization_generation_rejected(self):
        for bad in ('mat-9', 'sha256:' + 'a' * 64, 'mat-sha256:' + 'a' * 63,
                    'mat-sha256:' + 'Z' * 64, 'mat-md5:' + 'a' * 64):
            self._assert_unknown(
                self._mint(self._claims(materialization_generation=bad)),
                'bad_materialization_generation')

    # --- freshness attacks ---
    def test_expired_rejected(self):
        now = int(time.time())
        self._assert_unknown(self._mint(self._claims(iat=now - 10, nbf=now - 10, exp=now - 5)), 'expired')

    def test_not_yet_valid_rejected(self):
        # nbf in the future while iat<=now (a bounded ≤5s window whose nbf gate
        # has not opened yet) — the `not_yet_valid` gate, distinct from iat_in_future.
        now = int(time.time())
        self._assert_unknown(self._mint(self._claims(iat=now, nbf=now + 2, exp=now + 3)), 'not_yet_valid')

    def test_lifetime_over_five_seconds_rejected(self):
        now = int(time.time())
        self._assert_unknown(self._mint(self._claims(iat=now, nbf=now, exp=now + 6)), 'lifetime_too_long')

    def test_missing_jti_rejected(self):
        c = self._claims()
        c.pop('jti')
        self._assert_unknown(self._mint(c), 'missing_claim')

    # --- HIVE-1153 P2 #1: exact JOSE header + duplicate-claim + future-iat matrix ---
    def _raw_token(self, header_bytes, payload_bytes, sig=b'unchecked-at-step-0'):
        """A hand-assembled compact JWS from RAW header/payload bytes, so the test
        can inject duplicate JSON keys PyJWT would never emit. The structural /
        duplicate / header-shape gates all fire BEFORE signature verification, so
        the signature here is deliberately irrelevant."""
        import base64 as _b64

        def _seg(raw):
            return _b64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')
        return f'{_seg(header_bytes)}.{_seg(payload_bytes)}.{_seg(sig)}'

    def test_extra_protected_header_key_rejected(self):
        # A validly SIGNED token that smuggles an extra `jku` header is rejected
        # outright, not ignored (the exact-head adversarial akira reproduced).
        tok = _jwt.encode(self._claims(), self.priv_pem, algorithm='EdDSA',
                          headers={'typ': ce.EVIDENCE_TYP, 'kid': self.KID,
                                   'jku': 'https://evil.example/keys'})
        self._assert_unknown(tok, 'unexpected_header_keys')

    def test_duplicate_header_claim_rejected(self):
        import json as _json
        header = b'{"alg":"EdDSA","typ":"%b","kid":"%b","kid":"other"}' % (
            ce.EVIDENCE_TYP.encode(), self.KID.encode())
        payload = _json.dumps(self._claims()).encode()
        self._assert_unknown(self._raw_token(header, payload), 'duplicate_header_claim')

    def test_duplicate_payload_claim_rejected(self):
        import json as _json
        header = _json.dumps(
            {'alg': 'EdDSA', 'typ': ce.EVIDENCE_TYP, 'kid': self.KID}).encode()
        # A benign visible sub plus a shadowing duplicate.
        payload = (b'{"sub":"%b","sub":"attacker@evil.com"}'
                   % self.email.encode())
        self._assert_unknown(self._raw_token(header, payload), 'duplicate_payload_claim')

    def test_future_iat_rejected_even_within_nbf_exp(self):
        # akira's exact-head repro: iat=now+4, nbf=now, exp=now+5 — nbf<=now<exp
        # holds, but an issued-in-the-future token is never fresh (RFC 8725).
        now = int(time.time())
        self._assert_unknown(
            self._mint(self._claims(iat=now + 4, nbf=now, exp=now + 5)), 'iat_in_future')

    def test_non_positive_lifetime_rejected(self):
        now = int(time.time())
        self._assert_unknown(
            self._mint(self._claims(iat=now, nbf=now, exp=now)), 'non_positive_lifetime')

    # --- fail-closed on unwired pins ---
    def test_no_pinned_keys_fails_closed(self):
        with self.assertRaises(ce.EvidenceAuthorityUnknown) as ctx:
            ce.verify_evidence_jws(
                self._mint(), request=self.request, expected_email=self.email,
                expected_org_id=self.org, expected_team_slug=self.team,
                requirement_key=self.reqkey, required_capabilities=self.required,
                pinned_jwks={}, pinned_generation=self.pinned_gen)
        self.assertEqual(ctx.exception.reason, 'no_pinned_keys')


class SubstrateCapModeTests(SimpleTestCase):
    def test_missing_mode_rejected(self):
        with self.assertRaises(ce.SubstrateCapModeError):
            ce.get_substrate_cap_mode({})

    def test_blank_mode_rejected(self):
        with self.assertRaises(ce.SubstrateCapModeError):
            ce.get_substrate_cap_mode({'PULSE_SUBSTRATE_CAP_MODE': '  '})

    def test_valid_modes(self):
        for m in ('off', 'shadow', 'enforce'):
            self.assertEqual(ce.get_substrate_cap_mode({'PULSE_SUBSTRATE_CAP_MODE': m}), m)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ce.SubstrateCapModeError):
            ce.get_substrate_cap_mode({'PULSE_SUBSTRATE_CAP_MODE': 'audit'})

    def test_retired_boolean_dual_config_rejected(self):
        with self.assertRaises(ce.SubstrateCapModeError):
            ce.get_substrate_cap_mode({'PULSE_SUBSTRATE_CAP_MODE': 'enforce',
                                       'PULSE_SUBSTRATE_CAP_ENFORCE': '1'})

    def test_canonical_request_hash_is_stable_and_sorted(self):
        a = ce.build_evidence_request(
            email='X@Shizuha.com', organization_id=1, team_slug='devops',
            requirement_key='k', required_capabilities=['kubeconfig', 'host-exec'],
            item_id=1, assignment_generation=1, request_id='r1')
        self.assertEqual(a['email'], 'x@shizuha.com')
        self.assertEqual(a['required_capabilities'], ['host-exec', 'kubeconfig'])
        self.assertEqual(ce.request_sha256(a), ce.request_sha256(dict(reversed(list(a.items())))))


class SubstrateShadowTelemetryTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name='Shadow telemetry', key='SHT', org_id=1, access_level='open')
        self.item = Item.objects.create(
            mode='task', title='shadow telemetry', project=self.project,
            workflow_name='', assignment_group='engineering')
        self.item.refresh_from_db()

    @mock.patch.dict(os.environ, {'PULSE_SUBSTRATE_CAP_MODE': 'shadow'})
    def test_shadow_breadcrumb_is_durable_cross_replica_and_metric_bounded(self):
        verdict = ce.SubstrateVerdict(
            ce.MODE_SHADOW, ce.OUTCOME_AUTHORITY_UNKNOWN, 'producer_unreachable')
        for _replica in range(2):
            WorkflowService._emit_substrate_shadow_breadcrumb(
                'mio@shizuha.com', self.item, verdict, legacy_ok=True,
                required_caps={'kubeconfig'})

        rows = RoutingDenialAudit.objects.filter(
            action='substrate_capability_shadow')
        self.assertEqual(rows.count(), 1)
        breadcrumb = rows.get()
        self.assertEqual(breadcrumb.reason, 'producer_unreachable')
        self.assertTrue(breadcrumb.evidence['mismatch'])
        self.assertEqual(
            breadcrumb.evidence['schema_version'],
            'pulse.substrate-capability-shadow.v1')
        self.assertNotIn('mio@shizuha.com', json.dumps(breadcrumb.evidence))
        aggregate = SubstrateShadowMetric.objects.get()
        self.assertEqual(aggregate.observations, 1)

        metrics = WorkflowService.render_substrate_shadow_metrics()
        self.assertIn('pulse_resolver_substrate_mode{mode="shadow"} 1', metrics)
        self.assertIn(
            'pulse_resolver_substrate_shadow_observations_total'
            '{outcome="authority_unknown",reason="producer_unreachable",mismatch="true"} 1',
            metrics)


class SubstrateVerdictTests(CapabilityEvidenceVerifierTests):
    """Mode-aware verdict + producer fetch, reusing the keypair/pinning fixtures."""

    def _evaluate(self, mode, fetch):
        return ce.evaluate_substrate(
            mode=mode, request=self.request, expected_email=self.email,
            expected_org_id=self.org, expected_team_slug=self.team,
            requirement_key=self.reqkey, required_capabilities=self.required,
            fetch=fetch, pinned_jwks=self.pinned_jwks, pinned_generation=self.pinned_gen)

    def test_off_short_circuits_without_fetch(self):
        def _no_fetch(request, **kw):
            raise AssertionError('off mode must not fetch')
        v = ce.evaluate_substrate(
            mode=ce.MODE_OFF, request=self.request, expected_email=self.email,
            expected_org_id=self.org, expected_team_slug=self.team,
            requirement_key=self.reqkey, required_capabilities=self.required,
            fetch=_no_fetch, pinned_jwks=self.pinned_jwks, pinned_generation=self.pinned_gen)
        self.assertTrue(v.is_off)
        self.assertFalse(v.authorizes)

    def test_shadow_authorized_on_present_evidence(self):
        tok = self._mint()
        v = self._evaluate(ce.MODE_SHADOW, lambda request, **kw: tok)
        self.assertEqual(v.outcome, ce.OUTCOME_AUTHORIZED)
        self.assertTrue(v.authorizes)

    def test_enforce_and_shadow_compute_identical_verdict(self):
        tok = self._mint()
        s = self._evaluate(ce.MODE_SHADOW, lambda request, **kw: tok)
        e = self._evaluate(ce.MODE_ENFORCE, lambda request, **kw: tok)
        self.assertEqual((s.outcome, s.present_capabilities),
                         (e.outcome, e.present_capabilities))

    def test_denied_on_absent_capability(self):
        tok = self._mint(self._claims(results={'kubeconfig': 'ABSENT'}))
        v = self._evaluate(ce.MODE_ENFORCE, lambda request, **kw: tok)
        self.assertEqual(v.outcome, ce.OUTCOME_DENIED)
        self.assertFalse(v.authorizes)
        self.assertEqual(v.reason, 'seat_incapable_substrate')

    def test_authority_unknown_when_producer_unreachable(self):
        def _raise(request, **kw):
            raise ce.EvidenceAuthorityUnknown('producer_unreachable')
        v = self._evaluate(ce.MODE_ENFORCE, _raise)
        self.assertEqual(v.outcome, ce.OUTCOME_AUTHORITY_UNKNOWN)
        self.assertFalse(v.authorizes)
        self.assertEqual(v.reason, 'producer_unreachable')

    def test_authority_unknown_on_tampered_evidence(self):
        tok = self._mint(self._claims(sub='kane@shizuha.com'))
        v = self._evaluate(ce.MODE_ENFORCE, lambda request, **kw: tok)
        self.assertEqual(v.outcome, ce.OUTCOME_AUTHORITY_UNKNOWN)
        self.assertEqual(v.reason, 'wrong_subject')

    def test_fetch_no_url_fails_closed(self):
        with self.assertRaises(ce.EvidenceAuthorityUnknown) as ctx:
            ce.fetch_evidence(self.request, url='', environ={})
        self.assertEqual(ctx.exception.reason, 'no_producer_url')

    def test_fetch_no_token_fails_closed(self):
        # HIVE-1153 P1: without the shared internal token the real Hive view
        # returns 403 — Pulse must fail closed BEFORE making the call.
        with self.assertRaises(ce.EvidenceAuthorityUnknown) as ctx:
            ce.fetch_evidence(
                self.request, url='http://hive.internal/evidence',
                environ={'PULSE_HIVE_EVIDENCE_TOKEN': ''})
        self.assertEqual(ctx.exception.reason, 'no_producer_token')

    def test_fetch_transport_sends_canonical_bytes_and_token(self):
        # HIVE-1153 P1 (transport regression): the POST body MUST be the
        # byte-exact canonical form Hive re-hashes into request_sha256 — NOT
        # requests' default `json=` spacing (which the view rejects as
        # HIVE_EVIDENCE_REQUEST_NOT_CANONICAL) — carried with the shared
        # `X-Hive-Internal-Token` and an explicit JSON content type.
        captured = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self_inner):
                return None

            def json(self_inner):
                return {'evidence': 'compact.jws.value'}

        def _fake_post(endpoint, data=None, timeout=None, headers=None, **kw):
            captured.update(endpoint=endpoint, data=data, timeout=timeout,
                            headers=headers or {}, kw=kw)
            return _Resp()

        import requests as _requests
        with mock.patch.object(_requests, 'post', _fake_post):
            jws = ce.fetch_evidence(
                self.request, url='http://hive.internal/evidence',
                environ={'PULSE_HIVE_EVIDENCE_TOKEN': 'shhh-internal'})

        self.assertEqual(jws, 'compact.jws.value')
        # Exact canonical body — never the non-canonical json= path.
        self.assertNotIn('json', captured['kw'])
        self.assertEqual(captured['data'], ce.canonical_request_bytes(self.request))
        # The transmitted body's own hash equals the SHA the signed claims bind.
        self.assertEqual(
            'sha256:' + hashlib.sha256(captured['data']).hexdigest(),
            ce.request_sha256(self.request))
        self.assertEqual(captured['headers'].get('Content-Type'), 'application/json')
        self.assertEqual(captured['headers'].get('X-Hive-Internal-Token'), 'shhh-internal')
        self.assertEqual(captured['headers'].get('X-Internal-Service'), 'shizuha-pulse')

    def test_fetch_typed_409_surfaces_conflict_code(self):
        # PLAT-9505: a typed producer conflict (the 409 class —
        # HIVE_EVIDENCE_SUBJECT_MISMATCH / SUBJECT_NOT_MATERIALIZED /
        # EXACT_SEAT_MISMATCH) is a diagnosable strand, not unreachability.
        # The reason slug must carry the API's error code so a persistent
        # conflict condition self-identifies in deduplicated breadcrumbs
        # instead of masquerading as producer_unreachable. Fail-closed
        # outcome unchanged.
        class _Resp:
            status_code = 409

            def json(self_inner):
                return {'error': 'HIVE_EVIDENCE_SUBJECT_MISMATCH'}

        def _fake_post(endpoint, data=None, timeout=None, headers=None, **kw):
            return _Resp()

        import requests as _requests
        with mock.patch.object(_requests, 'post', _fake_post):
            with self.assertRaises(ce.EvidenceAuthorityUnknown) as ctx:
                ce.fetch_evidence(
                    self.request, url='http://hive.internal/evidence',
                    environ={'PULSE_HIVE_EVIDENCE_TOKEN': 'shhh-internal'})
        self.assertEqual(
            ctx.exception.reason,
            'producer_http_409_hive_evidence_subject_mismatch')

    def test_fetch_http_error_without_code_falls_back_to_status_slug(self):
        # An unparseable error body still fails closed with the status-class
        # slug — never producer_unreachable (the endpoint WAS reachable).
        class _Resp:
            status_code = 503

            def json(self_inner):
                raise ValueError('not json')

        def _fake_post(endpoint, data=None, timeout=None, headers=None, **kw):
            return _Resp()

        import requests as _requests
        with mock.patch.object(_requests, 'post', _fake_post):
            with self.assertRaises(ce.EvidenceAuthorityUnknown) as ctx:
                ce.fetch_evidence(
                    self.request, url='http://hive.internal/evidence',
                    environ={'PULSE_HIVE_EVIDENCE_TOKEN': 'shhh-internal'})
        self.assertEqual(ctx.exception.reason, 'producer_http_503')

    def test_fetch_non_fleet_form_seat_shortcircuits_without_roundtrip(self):
        """PLAT-9505 ruling (b) (nagi, HIVE-1153/PLAT-4732 owner): a seat that
        cannot possibly canonicalize to a fleet subject (non-@shizuha.com form)
        gets a typed authority_unknown WITHOUT the doomed round-trip — the
        producer would deterministically 409. Narrow: identity FORM only, so
        transient hive faults on fleet-form seats still round-trip (pinned by
        the transport test's mio@shizuha.com round-trip). Fail-closed
        unchanged: no evidence minted, no capability granted."""
        request = ce.build_evidence_request(
            email='operator@example.test', organization_id=self.org,
            team_slug=self.team, requirement_key=self.reqkey,
            required_capabilities=self.required,
            item_id=24160, assignment_generation=7,
            request_id='22222222-2222-2222-2222-222222222222')

        def _fail_post(*args, **kw):
            raise AssertionError('round-trip must be eliminated for non-fleet-form seats')

        import requests as _requests
        with mock.patch.object(_requests, 'post', _fail_post):
            with self.assertRaises(ce.EvidenceAuthorityUnknown) as ctx:
                ce.fetch_evidence(
                    request, url='http://hive.internal/evidence',
                    environ={'PULSE_HIVE_EVIDENCE_TOKEN': 'shhh-internal'})
        self.assertEqual(ctx.exception.reason, 'non_fleet_form_seat')
        self.assertIn('operator@example.test', ctx.exception.detail)


_GOLDEN_PATH = os.path.join(os.path.dirname(__file__), 'fixtures',
                            'hive_capability_evidence_v2_golden.json')


class GoldenVectorCrossCheckTests(SimpleTestCase):
    """HIVE-1153 <-> PLAT-4732 byte-exact reconciliation via the shared SHA-pinned
    golden vector. BOTH repos consume this identical fixture; the pinned SHAs are
    the cross-repo agreement — a drift in the request shape, canonical hashing, or
    the signed claims on EITHER side fails here. HLD PLS-277 v18 §5.2 acceptance #4.
    """
    EXPECTED_VECTOR_SHA = 'sha256:15fc74daa8fea53ce4a5539eeccd3016e6b3eb7bffe9a64bb4b16d655a2fe2ab'
    EXPECTED_REQUEST_SHA = 'sha256:63b27d99cb89a0fab8f1309952d2a427c3e03edc2f5f936346bd4a926c376564'

    def setUp(self):
        with open(_GOLDEN_PATH) as fh:
            self.g = json.load(fh)

    def _pinned(self):
        jwk = self.g['public_jwk']
        pinned_jwks = {jwk['kid']: _jwt.algorithms.OKPAlgorithm.from_jwk(json.dumps(jwk))}
        gen = self.g['producer_generation']
        pinned_gen = {'producer_generation_id': gen['producer_generation_id'],
                      'producer_generation_digest': gen['producer_generation_digest'],
                      'kid': jwk['kid']}
        return pinned_jwks, pinned_gen

    def _verify(self, **over):
        c = self.g['claims']
        kw = dict(request=self.g['request'], expected_email=c['sub'],
                  expected_org_id=c['organization_id'], expected_team_slug=c['team_slug'],
                  requirement_key=c['requirement_key'],
                  required_capabilities=c['required_capabilities'])
        pinned_jwks, pinned_gen = self._pinned()
        kw.update(pinned_jwks=pinned_jwks, pinned_generation=pinned_gen)
        kw.update(over)
        return ce.verify_evidence_jws(self.g['compact_jws'], **kw)

    def test_vector_and_request_sha_pinned(self):
        # Pinned cross-repo SHAs; my canonical hashing reproduces the producer's.
        self.assertEqual(self.g['vector_sha256'], self.EXPECTED_VECTOR_SHA)
        self.assertEqual(self.g['request_sha256'], self.EXPECTED_REQUEST_SHA)
        self.assertEqual(ce.request_sha256(self.g['request']), self.g['request_sha256'])

    def test_golden_jws_verifies_within_window(self):
        ev = self._verify(now=int(self.g['claims']['iat']) + 2)
        self.assertEqual(ev.present_capabilities, frozenset({'host-exec', 'kubeconfig'}))
        self.assertTrue(ev.satisfies(['kubeconfig', 'host-exec']))

    def test_golden_jws_expired_at_real_time(self):
        # Freshness is REAL: the frozen golden token is expired under wall-clock.
        with self.assertRaises(ce.EvidenceAuthorityUnknown) as ctx:
            self._verify()
        self.assertEqual(ctx.exception.reason, 'expired')

    def test_golden_request_tamper_breaks_sha_binding(self):
        # Flip one request field -> request_sha256 no longer matches the signed claim.
        tampered = dict(self.g['request'], organization_id=999)
        with self.assertRaises(ce.EvidenceAuthorityUnknown) as ctx:
            self._verify(request=tampered, now=int(self.g['claims']['iat']) + 2)
        self.assertEqual(ctx.exception.reason, 'wrong_request_sha256')


class RequirementKeyV2Tests(SimpleTestCase):
    """HIVE-1153 P1 #3 (HLD PLS-277 v20 §5.2): the strict typed content-addressed
    RequirementKey encoder + the workflow-policy-version digest. Pure functions;
    the model-side envelope builder + commit-lock revalidation are covered by the
    DB-backed workflow-policy regression."""

    WF = '11111111-2222-3333-4444-555555555555'
    STAGE = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    POL = 'sha256:' + 'c' * 64

    def _key(self, **over):
        kw = dict(organization_id=1, workflow_id=self.WF, policy_version=self.POL,
                  stage_status_id=self.STAGE, assignment_intent='assign')
        kw.update(over)
        return ce.build_requirement_key_v2(**kw)

    def test_encoder_exact_shape(self):
        self.assertEqual(
            self._key(),
            f'rk2:org=1;wf={self.WF};pol={self.POL};stage={self.STAGE};intent=assign')

    def test_encoder_lowercases_and_ints(self):
        k = self._key(organization_id='7', workflow_id=self.WF.upper(),
                      policy_version=self.POL.upper())
        self.assertEqual(
            k, f'rk2:org=7;wf={self.WF};pol={self.POL};stage={self.STAGE};intent=assign')

    def test_encoder_rejects_non_uuid_workflow(self):
        with self.assertRaises(ValueError):
            self._key(workflow_id='simple')

    def test_encoder_rejects_non_uuid_stage(self):
        with self.assertRaises(ValueError):
            self._key(stage_status_id='in_progress')

    def test_encoder_rejects_bad_policy_digest(self):
        for bad in ('c' * 64, 'sha256:' + 'c' * 63, 'md5:' + 'c' * 64, 'sha256:ZZZ'):
            with self.assertRaises(ValueError):
                self._key(policy_version=bad)

    def test_encoder_rejects_unknown_intent(self):
        with self.assertRaises(ValueError):
            self._key(assignment_intent='escalate')

    def test_encoder_intents_are_closed_set(self):
        for good in ('assign', 'transfer', 'resume', 'retain', 'system'):
            self.assertIn(f'intent={good}', self._key(assignment_intent=good))

    def test_delimiters_never_smuggled(self):
        # No validated component can contain the ; or = framing separators.
        k = self._key()
        self.assertEqual(k.count(';'), 4)
        self.assertEqual(len([p for p in k.split(';')]), 5)

    # --- workflow_policy_version digest ---
    def _env(self, **over):
        env = {'schema': ce.WORKFLOW_POLICY_ENVELOPE_SCHEMA,
               'normalizer_version': ce.WORKFLOW_POLICY_NORMALIZER_VERSION,
               'workflow': {'id': self.WF, 'slug': 'simple', 'is_active': True,
                            'is_system': False},
               'statuses': [{'membership_id': self.STAGE, 'slug': 'open', 'position': 0}],
               'transitions': [{'id': self.WF, 'from_status': 'open', 'to_status': 'done',
                                'assignee_only': True}]}
        env.update(over)
        return env

    def test_policy_version_shape_and_determinism(self):
        d1 = ce.workflow_policy_version(self._env())
        d2 = ce.workflow_policy_version(self._env())
        self.assertTrue(_SHA_OK(d1))
        self.assertEqual(d1, d2)  # deterministic over identical envelopes

    def test_policy_version_ignores_key_order(self):
        reordered = dict(reversed(list(self._env().items())))
        self.assertEqual(ce.workflow_policy_version(self._env()),
                         ce.workflow_policy_version(reordered))

    def test_policy_version_changes_on_authorizing_field(self):
        base = ce.workflow_policy_version(self._env())
        flipped = self._env(transitions=[{'id': self.WF, 'from_status': 'open',
                                          'to_status': 'done', 'assignee_only': False}])
        self.assertNotEqual(base, ce.workflow_policy_version(flipped))

    def test_policy_version_requires_schema(self):
        with self.assertRaises(ValueError):
            ce.workflow_policy_version({'workflow': {}})

    def test_policy_version_rejects_floats(self):
        with self.assertRaises(ValueError):
            ce.workflow_policy_version(self._env(statuses=[{'position': 1.5}]))


def _SHA_OK(s):
    import re as _re
    return bool(_re.match(r'^sha256:[0-9a-f]{64}$', s))


class SubstrateReceiptRevalidationTests(SimpleTestCase):
    """HIVE-1153 P1 #3 increment 3 (architect ruling): the selection-time receipt
    is revalidated LOCALLY at the commit fence — no producer re-fetch. Proves each
    drift class (rk2/policy/stage/intent, writer generation, producer tuple, 5s
    expiry, subject) fails closed, and a still-fresh matching receipt holds."""

    RK = 'rk2:org=1;wf=' + '1' * 8 + '-1111-1111-1111-' + '1' * 12 + ';pol=sha256:' + 'a' * 64 + ';stage=' + '2' * 8 + '-2222-2222-2222-' + '2' * 12 + ';intent=assign'
    PGEN = {'producer_generation_id': 'gen-x', 'producer_generation_digest': 'sha256:dd', 'kid': 'k'}

    def _receipt(self, **over):
        base = dict(
            requirement_key=self.RK, request_sha256='sha256:r', assignment_generation=7,
            organization_id=1, team_slug='devops', subject='mio@shizuha.com',
            required_capabilities=('kubeconfig',), present_capabilities=frozenset({'kubeconfig'}),
            materialization_generation='mat-sha256:' + 'c' * 64,
            producer_generation_id='gen-x', producer_generation_digest='sha256:dd',
            jti='j', expires_at=1_000_000)
        base.update(over)
        return ce.SubstrateCapabilityReceipt(**base)

    def _reval(self, receipt=None, **over):
        kw = dict(recomputed_requirement_key=self.RK, current_assignment_generation=7,
                  pinned_generation=self.PGEN, expected_subject='mio@shizuha.com',
                  now=999_000)
        kw.update(over)
        return ce.revalidate_substrate_receipt(receipt or self._receipt(), **kw)

    def test_fresh_matching_receipt_holds(self):
        self.assertIsNone(self._reval())

    def test_expired_receipt_holds_closed(self):
        self.assertEqual(self._reval(now=1_000_001), 'receipt_expired')
        self.assertEqual(self._reval(self._receipt(expires_at=0)), 'receipt_expired')

    def test_requirement_key_drift(self):
        drifted = self.RK.replace('intent=assign', 'intent=transfer')
        self.assertEqual(self._reval(recomputed_requirement_key=drifted), 'requirement_key_drift')

    def test_writer_generation_drift(self):
        self.assertEqual(self._reval(current_assignment_generation=8), 'writer_generation_drift')

    def test_subject_drift(self):
        self.assertEqual(self._reval(expected_subject='kane@shizuha.com'), 'subject_drift')

    def test_producer_generation_drift(self):
        self.assertEqual(
            self._reval(pinned_generation=dict(self.PGEN, producer_generation_id='gen-old')),
            'producer_generation_drift')
        self.assertEqual(
            self._reval(pinned_generation=dict(self.PGEN, producer_generation_digest='sha256:zz')),
            'producer_generation_drift')

    # HIVE-1153 P1 (akira re-review): full authorizing-context drift at commit.
    def test_team_slug_drift(self):
        # receipt team_slug is 'devops'; an assignment-group change to 'review' HOLDs.
        self.assertEqual(self._reval(expected_team_slug='review'), 'team_slug_drift')

    def test_required_capabilities_drift(self):
        # receipt required_capabilities is ('kubeconfig',); an instance capability
        # addition/change (host-exec) that the rk2 policy digest does not capture HOLDs.
        self.assertEqual(
            self._reval(expected_required_capabilities=['kubeconfig', 'host-exec']),
            'required_capabilities_drift')

    def test_org_drift(self):
        self.assertEqual(self._reval(expected_org_id=999), 'org_drift')

    def test_full_context_match_holds(self):
        self.assertIsNone(self._reval(
            expected_team_slug='devops', expected_required_capabilities=['kubeconfig'],
            expected_org_id=1))

    def test_build_receipt_from_verified_evidence(self):
        ev = ce.VerifiedEvidence(
            subject='mio@shizuha.com', organization_id=1, team_slug='devops',
            request_sha256='sha256:r', required_capabilities=('kubeconfig',),
            present_capabilities=frozenset({'kubeconfig'}),
            materialization_generation='mat-sha256:' + 'c' * 64,
            producer_generation_id='gen-x', jti='j',
            producer_generation_digest='sha256:dd', expires_at=1_000_000)
        request = {'requirement_key': self.RK, 'assignment_generation': 7}
        r = ce.build_substrate_receipt(ev, request)
        self.assertEqual(r.requirement_key, self.RK)
        self.assertEqual(r.assignment_generation, 7)
        self.assertEqual(r.producer_generation_digest, 'sha256:dd')
        self.assertEqual(r.expires_at, 1_000_000)
        # a freshly-built receipt revalidates clean against its own inputs
        self.assertIsNone(ce.revalidate_substrate_receipt(
            r, recomputed_requirement_key=self.RK, current_assignment_generation=7,
            pinned_generation=self.PGEN, expected_subject='mio@shizuha.com', now=999_000))
