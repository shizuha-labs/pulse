"""PLAT-12577: role-qualified callers mint without a user-delegation token.

The decision is pure (no Django). Pulse still authorizes the roster; this only
pins that the MCP adapter stops refusing the named roles before that check,
and that automation identities stay fail-closed.
"""
import base64
import json
import unittest

import waiver_authority


def _unsigned_jwt(claims: dict) -> str:
    def segment(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment(claims)}."


class WaiverAuthorityTests(unittest.TestCase):
    def test_each_named_waiver_role_uses_the_caller_token(self):
        for role in ("operator", "admin-ops", "devops", "qa"):
            with self.subTest(role=role):
                token = _unsigned_jwt({
                    "email": "goro@agents.shizuha.io",
                    "roles": [role],
                })
                self.assertTrue(waiver_authority.caller_may_mint_deploy_waiver(token))

    def test_team_claim_and_operator_boolean_are_authority(self):
        team = _unsigned_jwt({"email": "owner@shizuha.com", "team": "devops"})
        operator = _unsigned_jwt({"email": "hritik@shizuha.com", "is_operator": True})
        self.assertTrue(waiver_authority.caller_may_mint_deploy_waiver(team))
        self.assertTrue(waiver_authority.caller_may_mint_deploy_waiver(operator))

    def test_undelegated_caller_without_a_role_stays_fail_closed(self):
        token = _unsigned_jwt({"email": "saki@shizuha.com", "roles": ["engineering"]})
        self.assertFalse(waiver_authority.caller_may_mint_deploy_waiver(token))
        self.assertFalse(waiver_authority.caller_may_mint_deploy_waiver(""))
        self.assertFalse(waiver_authority.caller_may_mint_deploy_waiver("not-a-jwt"))

    def test_automation_identity_never_mints_on_its_own_token(self):
        for claims in (
            {"email": "system@pulse", "roles": ["admin-ops"]},
            {"email": "workflow@pulse", "team": "devops"},
            {"email": "scheduler@pulse.shizuha.com", "roles": ["qa"]},
            {"email": "pr-sync@shizuha.local", "roles": ["operator"]},
            {"email": "hook@shizuha.com", "token_use": "automation", "roles": ["operator"]},
            {"email": "ingest@shizuha.com", "principal_kind": "service", "team": "devops"},
            {"email": "alerts@shizuha.com", "identity_kind": "system", "groups": ["qa"]},
        ):
            with self.subTest(claims=claims):
                self.assertFalse(
                    waiver_authority.caller_may_mint_deploy_waiver(_unsigned_jwt(claims))
                )


if __name__ == "__main__":
    unittest.main()
