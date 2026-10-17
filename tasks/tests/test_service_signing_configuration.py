import os
import secrets
from unittest.mock import patch
from django.test import SimpleTestCase
from tasks.services.service_signing import configured_service_signing_key


class ServiceSigningConfigurationTests(SimpleTestCase):
    def test_configured_key_is_used_exactly(self):
        value = secrets.token_urlsafe(32)
        with patch.dict(os.environ, {'JWT_SECRET_KEY': value}):
            self.assertEqual(configured_service_signing_key(), value)

    def test_admin_team_callers_do_not_mint_tokens_without_configured_key(self):
        from tasks.tasks import _org_health_admin_team_snapshot
        from tasks.views import ItemViewSet, OrgChartSnapshotView
        ItemViewSet._admin_teams_cache.clear()
        for value in ['', '   ']:
            with self.subTest(value=value), patch.dict(os.environ, {'JWT_SECRET_KEY': value}), \
                 patch('jwt.encode') as encode, patch('requests.get') as get:
                self.assertIsNone(ItemViewSet()._get_admin_teams(1, 'demo'))
                self.assertEqual(OrgChartSnapshotView()._fetch_admin_teams('demo'), [])
                result = _org_health_admin_team_snapshot(1, 'demo', 'review')
                self.assertEqual(result['status'], 'unavailable')
                encode.assert_not_called()
                get.assert_not_called()
