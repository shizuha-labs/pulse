"""PLS-612 regression: paginated Pulse API links must advertise the public
prefix (/pulse/) and the caller's scheme, not the internal /shizuha-pulse/
prefix the reverse proxy rewrites to.

The proxy rewrites /pulse/ -> /shizuha-pulse/ before the request reaches
Django, so DRF's build_absolute_uri() emits next/previous under
/shizuha-pulse/. Clients following those links verbatim leave the caller's
path namespace (and, with the proxy scheme bug, HTTPS). The pagination
override in tasks.pagination normalises the advertised prefix back to the
public one; the scheme is governed by SECURE_PROXY_SSL_HEADER once the proxy
forwards X-Forwarded-Proto: https.
"""
import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=True,
        SECRET_KEY='test-secret',
        FORCE_SCRIPT_NAME='/shizuha-pulse',
        PUBLIC_API_PREFIX='/pulse',
        REST_FRAMEWORK={},
        INSTALLED_APPS=[
            'django.contrib.contenttypes',
            'django.contrib.auth',
            'rest_framework',
        ],
    )
    django.setup()

from unittest.mock import Mock  # noqa: E402

from django.test import SimpleTestCase, override_settings  # noqa: E402

from tasks.pagination import StandardPagination, _public_uri  # noqa: E402


class PublicUriTests(SimpleTestCase):
    def test_internal_prefix_is_rewritten_to_public_prefix(self):
        url = 'http://shizuha-nginx.shizuha.svc.cluster.local/shizuha-pulse/api/workflows/?page=2'
        self.assertEqual(
            _public_uri(url),
            'http://shizuha-nginx.shizuha.svc.cluster.local/pulse/api/workflows/?page=2',
        )

    def test_public_prefix_url_is_unchanged(self):
        url = 'https://shizuha.com/pulse/api/workflows/?page=2'
        self.assertEqual(_public_uri(url), url)

    def test_none_passthrough(self):
        self.assertIsNone(_public_uri(None))

    def test_settings_driven_rewrite(self):
        url = 'https://shizuha.com/shizuha-pulse/api/workflows/?page=2'
        self.assertEqual(
            _public_uri(url),
            'https://shizuha.com/pulse/api/workflows/?page=2',
        )


class PaginationLinkTests(SimpleTestCase):
    def _paginator(self, path, proto='https', host='shizuha.com'):
        request = Mock()
        request.build_absolute_uri.return_value = (
            f'{proto}://{host}{path}?page=1'
        )
        request.scheme = proto
        request.META = {'HTTP_X_FORWARDED_PROTO': proto}
        paginator = StandardPagination()
        paginator.request = request
        paginator.page = Mock(has_next=Mock(return_value=True))
        paginator.page.next_page_number.return_value = 2
        paginator.page.has_previous = Mock(return_value=True)
        paginator.page.previous_page_number.return_value = 1
        paginator.page_query_param = 'page'
        return paginator

    def test_next_link_advertises_public_prefix_and_https(self):
        paginator = self._paginator(
            '/shizuha-pulse/api/workflows/', proto='https'
        )
        next_url = paginator.get_next_link()
        self.assertTrue(next_url.startswith('https://shizuha.com/'), next_url)
        self.assertIn('/pulse/api/workflows/', next_url)
        self.assertNotIn('/shizuha-pulse/', next_url)
        self.assertIn('page=2', next_url)

    def test_previous_link_advertises_public_prefix(self):
        paginator = self._paginator(
            '/shizuha-pulse/api/workflows/?page=2', proto='https'
        )
        prev_url = paginator.get_previous_link()
        self.assertTrue(prev_url.startswith('https://shizuha.com/'), prev_url)
        self.assertIn('/pulse/api/workflows/', prev_url)
        self.assertNotIn('/shizuha-pulse/', prev_url)
