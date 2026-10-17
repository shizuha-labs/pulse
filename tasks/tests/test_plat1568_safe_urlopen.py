"""PLAT-1568/1570/1572/1574/1576 (bandit B310): safe_urlopen restricts the URL
scheme to http/https before delegating to urllib, closing the "file://" /
custom-scheme surface at every Pulse urlopen call site."""
from unittest.mock import patch

from django.test import SimpleTestCase

from tasks.net_utils import safe_urlopen


class SafeUrlopenSchemeGuardTests(SimpleTestCase):
    def test_http_and_https_pass_through(self):
        with patch('tasks.net_utils.urllib.request.urlopen', return_value='ok') as m:
            self.assertEqual(safe_urlopen('http://example.com/x', timeout=5), 'ok')
            self.assertEqual(safe_urlopen('https://example.com/x', timeout=5), 'ok')
        self.assertEqual(m.call_count, 2)

    def test_file_scheme_is_refused_without_opening(self):
        with patch('tasks.net_utils.urllib.request.urlopen') as m:
            with self.assertRaises(ValueError):
                safe_urlopen('file:///etc/passwd', timeout=5)
        m.assert_not_called()

    def test_ftp_and_custom_schemes_refused(self):
        with patch('tasks.net_utils.urllib.request.urlopen') as m:
            for bad in ('ftp://host/x', 'gopher://host/x', 'data:text/plain,hi', 'jar:file:///x'):
                with self.assertRaises(ValueError):
                    safe_urlopen(bad, timeout=5)
        m.assert_not_called()

    def test_accepts_request_object_and_reads_full_url(self):
        import urllib.request
        req = urllib.request.Request('https://example.com/api', method='POST')
        with patch('tasks.net_utils.urllib.request.urlopen', return_value='ok') as m:
            self.assertEqual(safe_urlopen(req, timeout=3), 'ok')
        m.assert_called_once()
        # A Request carrying a file:// URL is refused just like a bare string.
        bad_req = urllib.request.Request('file:///etc/hosts')
        with patch('tasks.net_utils.urllib.request.urlopen') as m:
            with self.assertRaises(ValueError):
                safe_urlopen(bad_req, timeout=3)
        m.assert_not_called()

    def test_kwargs_forwarded(self):
        with patch('tasks.net_utils.urllib.request.urlopen', return_value='ok') as m:
            safe_urlopen('https://example.com', timeout=9)
        _, kwargs = m.call_args
        self.assertEqual(kwargs.get('timeout'), 9)
