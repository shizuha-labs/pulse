"""ID-90: browser login cookie must authenticate when no Bearer header is sent."""

from django.test import RequestFactory, TestCase, override_settings
from rest_framework.exceptions import PermissionDenied

from tasks.authentication import ShizuhaJWTAuthentication


class CookieAuthTests(TestCase):
    def setUp(self):
        self.auth = ShizuhaJWTAuthentication()
        self.factory = RequestFactory()

    def test_cookie_is_used_when_authorization_header_is_absent(self):
        request = self.factory.get('/api/')
        request.COOKIES['shizuha-access-token'] = 'cookie-token'
        seen = {}

        def fake_validate(raw):
            seen['raw'] = raw
            return {'user_id': 7}

        self.auth.get_validated_token = fake_validate
        self.auth.get_user = lambda token: 'user'
        user, token = self.auth.authenticate(request)
        self.assertEqual(user, 'user')
        self.assertEqual(token, {'user_id': 7})
        self.assertEqual(seen['raw'], b'cookie-token')

    def test_authorization_header_wins_over_cookie(self):
        request = self.factory.get('/api/', HTTP_AUTHORIZATION='Bearer header-token')
        request.COOKIES['shizuha-access-token'] = 'cookie-token'
        called = {}

        def fake_super(self, req):
            called['header'] = req.META.get('HTTP_AUTHORIZATION')
            return ('header-user', 'header-token')

        self.auth.get_header = lambda req: b'Bearer header-token'
        # super().authenticate is the bearer path; stub via the parent by
        # replacing get_raw_token so a real header is not parsed as JWT.
        # PLAT-11893: super() binds the plain function to the instance, so the
        # fake receives (instance, request) — declare both.
        from rest_framework_simplejwt.authentication import JWTAuthentication
        JWTAuthentication.authenticate = fake_super
        try:
            user, token = self.auth.authenticate(request)
        finally:
            del JWTAuthentication.authenticate
        self.assertEqual((user, token), ('header-user', 'header-token'))
        self.assertIn('Bearer header-token', called['header'])

    def test_missing_cookie_is_anonymous(self):
        request = self.factory.get('/api/')
        self.assertIsNone(self.auth.authenticate(request))

    @override_settings(REST_AUTH={'JWT_AUTH_COOKIE': 'custom-access'})
    def test_cookie_name_comes_from_settings(self):
        request = self.factory.get('/api/')
        request.COOKIES['custom-access'] = 'named'
        self.auth.get_validated_token = lambda raw: {'raw': raw}
        self.auth.get_user = lambda token: 'user'
        user, token = self.auth.authenticate(request)
        self.assertEqual(token['raw'], b'named')
        self.assertEqual(user, 'user')

    def test_unsafe_cookie_request_requires_csrf(self):
        request = self.factory.post('/api/')
        request.COOKIES['shizuha-access-token'] = 'cookie-token'
        with self.assertRaises(PermissionDenied):
            self.auth.authenticate(request)
