"""Explicit deployment credentials for legacy service-token callers."""
import os
from django.core.exceptions import ImproperlyConfigured


def configured_service_signing_key():
    key = os.environ.get('JWT_SECRET_KEY', '')
    if not key.strip():
        raise ImproperlyConfigured('JWT_SECRET_KEY is required for service-token signing')
    return key
