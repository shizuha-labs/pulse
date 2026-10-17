from unittest.mock import MagicMock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from tasks.user_service import UserService


class UserServiceAgentAliasTests(SimpleTestCase):
    def _service(self, responses):
        svc = UserService()
        calls = []

        def _get(url, params=None, timeout=10):
            email = (params or {}).get("email")
            calls.append(email)
            resp = MagicMock()
            payload = responses.get(email)
            if payload is None:
                resp.status_code = 404
                return resp
            resp.status_code = 200
            resp.json.return_value = payload
            return resp

        svc.session.get = MagicMock(side_effect=_get)
        return svc, calls

    @override_settings(SHIZUHA_AGENT_API_URL="")
    def test_company_agent_alias_resolves_to_agents_domain_identity(self):
        svc, calls = self._service({
            "banto@agents.shizuha.io": {
                "id": 112,
                "username": "banto",
                "email": "banto@agents.shizuha.io",
            },
        })

        result = svc.get_user_by_email("banto@shizuha.com")

        self.assertEqual(result["id"], 112)
        self.assertEqual(result["email"], "banto@agents.shizuha.io")
        self.assertEqual(calls, ["banto@shizuha.com", "banto@agents.shizuha.io"])

    @override_settings(SHIZUHA_AGENT_API_URL="")
    def test_agents_domain_identity_uses_shizuha_id_before_legacy_agent_api(self):
        svc, calls = self._service({
            "banto@agents.shizuha.io": {
                "id": 112,
                "username": "banto",
                "email": "banto@agents.shizuha.io",
            },
        })

        result = svc.get_user_by_email("banto@agents.shizuha.io")

        self.assertEqual(result["id"], 112)
        self.assertEqual(result["email"], "banto@agents.shizuha.io")
        self.assertEqual(calls, ["banto@agents.shizuha.io"])

    def test_agent_identity_inventory_is_cached_with_email_aliases(self):
        cache.delete('shizuha_id_agent_identity_index_v1')
        svc = UserService()
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            'agents': [
                {
                    'user_id': 112,
                    'email': 'banto@shizuha.com',
                    'account_type': 'agent',
                },
            ],
        }
        svc.session.get = MagicMock(return_value=response)

        first = svc.get_agent_identity_index()
        second = svc.get_agent_identity_index()

        self.assertEqual(first['user_ids'], {112})
        self.assertEqual(
            first['emails'],
            {'banto@shizuha.com', 'banto@agents.shizuha.io'},
        )
        self.assertEqual(second, first)
        svc.session.get.assert_called_once()

    def test_agent_identity_inventory_failure_is_fail_closed_and_cached(self):
        cache.delete('shizuha_id_agent_identity_index_v1')
        svc = UserService()
        response = MagicMock()
        response.status_code = 503
        svc.session.get = MagicMock(return_value=response)

        self.assertIsNone(svc.get_agent_identity_index())
        self.assertIsNone(svc.get_agent_identity_index())
        svc.session.get.assert_called_once()
