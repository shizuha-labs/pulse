import json
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase
from rest_framework.exceptions import AuthenticationFailed

from tasks.middleware import ServiceAccessMiddleware


class ServiceAccessMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _middleware(self):
        return ServiceAccessMiddleware(lambda request: HttpResponse('ok'))

    def test_drive_only_token_is_denied_at_pulse_boundary(self):
        request = self.factory.get('/api/projects/', HTTP_AUTHORIZATION='Bearer token')
        with patch(
            'tasks.authentication.ShizuhaJWTAuthentication.get_validated_token',
            return_value={'enabled_services': ['id', 'drive']},
        ):
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 403)
        payload = json.loads(response.content)
        self.assertEqual(payload['code'], 'service_access_denied')
        self.assertEqual(payload['service'], 'pulse')

    def test_drive_only_token_is_denied_at_public_pulse_prefix(self):
        request = self.factory.get('/pulse/api/projects/', HTTP_AUTHORIZATION='Bearer token')
        with patch(
            'tasks.authentication.ShizuhaJWTAuthentication.get_validated_token',
            return_value={'enabled_services': ['id', 'drive']},
        ):
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 403)
        payload = json.loads(response.content)
        self.assertEqual(payload['code'], 'service_access_denied')
        self.assertEqual(payload['service'], 'pulse')

    def test_pulse_enabled_token_is_allowed_for_public_pulse_prefix(self):
        request = self.factory.get('/pulse/api/projects/', HTTP_AUTHORIZATION='Bearer token')
        with patch(
            'tasks.authentication.ShizuhaJWTAuthentication.get_validated_token',
            return_value={'enabled_services': ['id', 'pulse']},
        ):
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 200)

    def test_pulse_enabled_token_is_allowed(self):
        request = self.factory.get('/api/projects/', HTTP_AUTHORIZATION='Bearer token')
        with patch(
            'tasks.authentication.ShizuhaJWTAuthentication.get_validated_token',
            return_value={'enabled_services': ['id', 'pulse']},
        ):
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 200)

    def test_legacy_token_without_enabled_services_is_allowed(self):
        request = self.factory.get('/api/projects/', HTTP_AUTHORIZATION='Bearer token')
        with patch(
            'tasks.authentication.ShizuhaJWTAuthentication.get_validated_token',
            return_value={},
        ):
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 200)

    def test_invalid_token_is_left_for_drf_authentication(self):
        request = self.factory.get('/api/projects/', HTTP_AUTHORIZATION='Bearer bad')
        with patch(
            'tasks.authentication.ShizuhaJWTAuthentication.get_validated_token',
            side_effect=AuthenticationFailed('bad token'),
        ):
            response = self._middleware()(request)

        self.assertEqual(response.status_code, 200)
