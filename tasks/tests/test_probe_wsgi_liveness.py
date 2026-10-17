from unittest.mock import patch

from django.test import SimpleTestCase

from taskmanager import probe_wsgi


def _request(path="/livez/", method="GET"):
    response = {}

    def start_response(status, headers):
        response["status"] = status
        response["headers"] = dict(headers)

    response["body"] = b"".join(
        probe_wsgi.application(
            {"PATH_INFO": path, "REQUEST_METHOD": method},
            start_response,
        )
    )
    return response


class ProbeWsgiLivenessTests(SimpleTestCase):
    def test_liveness_fast_path_does_not_initialize_django(self):
        with patch.object(probe_wsgi, "_get_django_application") as get_django:
            response = _request()

        self.assertEqual(response["status"], "200 OK")
        self.assertEqual(
            response["body"],
            b'{"status": "ok", "listener": "probe"}',
        )
        self.assertEqual(response["headers"]["Content-Type"], "application/json")
        get_django.assert_not_called()

    def test_liveness_fast_path_rejects_non_get_without_django(self):
        with patch.object(probe_wsgi, "_get_django_application") as get_django:
            response = _request(method="POST")

        self.assertEqual(response["status"], "405 Method Not Allowed")
        self.assertEqual(response["headers"]["Allow"], "GET")
        get_django.assert_not_called()
