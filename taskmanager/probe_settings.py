"""Settings for Pulse's isolated, ClusterIP-only readiness listener."""

from .settings import *  # noqa: F401,F403


# This listener never serves login, registration, or social-auth endpoints.
# Keep those applications out of its Django registry as well as their request
# middleware out of the request path. django-allauth validates at app startup
# that AccountMiddleware is installed, so inheriting the public auth apps while
# intentionally setting MIDDLEWARE=[] makes the isolated listener fail to boot.
_PUBLIC_AUTH_APPS = {
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.github",
    "dj_rest_auth",
    "dj_rest_auth.registration",
}
INSTALLED_APPS = [app for app in INSTALLED_APPS if app not in _PUBLIC_AUTH_APPS]

ROOT_URLCONF = "taskmanager.probe_urls"
WSGI_APPLICATION = "taskmanager.probe_wsgi.application"
FORCE_SCRIPT_NAME = None

# The probe listener is not a scheduler owner.  Avoid AppConfig.ready() writes
# to django-celery-beat during every probe-process boot; those writes can block
# behind the backend/test workers and turn a healthy listener into a false
# startup timeout.
PULSE_BOOTSTRAP_PERIODIC_SCHEDULES = False

# The probe view performs every required pre-authentication guard itself.  The
# listener intentionally omits the public API/session/auth middleware surface.
MIDDLEWARE = []
