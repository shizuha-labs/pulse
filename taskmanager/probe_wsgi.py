"""WSGI entry point for the private workload-identity probe listener.

The kubelet liveness path must answer before Django finishes importing. Gunicorn
binds its socket before loading the application, and Pulse's Django startup can
take several minutes during a concurrent rollout. Routing ``/livez/`` directly
here prevents a healthy container from being killed while the heavier
authenticated DB-readiness application is still initializing.
"""

import os
import threading

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "taskmanager.probe_settings")

_django_application = None
_django_application_lock = threading.Lock()
_LIVE_BODY = b'{"status": "ok", "listener": "probe"}'


def _get_django_application():
    global _django_application
    if _django_application is None:
        with _django_application_lock:
            if _django_application is None:
                # Import lazily so /livez/ remains available while Django loads.
                # Do not initialize request tracing on this listener. Its
                # Authorization header is bearer material and must not appear
                # in traces.
                from django.core.wsgi import get_wsgi_application

                _django_application = get_wsgi_application()
    return _django_application


def application(environ, start_response):
    if environ.get("PATH_INFO") == "/livez/":
        if environ.get("REQUEST_METHOD") != "GET":
            start_response(
                "405 Method Not Allowed",
                [("Content-Length", "0"), ("Allow", "GET")],
            )
            return [b""]
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(_LIVE_BODY))),
            ],
        )
        return [_LIVE_BODY]
    return _get_django_application()(environ, start_response)
