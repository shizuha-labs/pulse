"""
WSGI config for taskmanager project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.2/howto/deployment/wsgi/
"""

import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'taskmanager.settings')

# Initialize OpenTelemetry before Django loads (if OTEL_ENABLED=true)
import taskmanager.otel_setup  # noqa: F401, E402

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
