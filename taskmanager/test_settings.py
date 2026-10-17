"""Hermetic CI settings for shizuha-pulse (PLAT-1358 / PLAT-1362).

CI must invoke Django tests with:
    python manage.py test tasks --settings=taskmanager.test_settings

The job owns local Postgres/Redis sidecars inside the test pod; no shared/prod
services are required. Cross-service APIs fast-fail on loopback unless tests
mock them explicitly.
"""
import os
import faulthandler

# The security switch has no production default: every real deployment must
# choose off|shadow|enforce explicitly before TasksConfig.ready() runs. This
# settings module is itself the explicit hermetic test deployment, so select
# the non-authorizing mode before importing INSTALLED_APPS.
os.environ.setdefault('PULSE_SUBSTRATE_CAP_MODE', 'off')

# Historical fixtures exercise the first-party alias migration contract.
# Production deployments supply their own independent trust anchors.
os.environ.setdefault('ORIGIN_PUBLIC_URL', 'https://origin.shizuha.com')
os.environ.setdefault('ORIGIN_PUBLIC_HOST_ALIASES', 'origin.shizuha')
os.environ.setdefault('ORIGIN_INTERNAL_HOST_ALIASES', 'forgejo-http.origin.svc,forgejo-http.origin.svc.cluster.local')
os.environ.setdefault('ORIGIN_LEGACY_HOST_ALIASES', 'git.shizuha.com')
os.environ.setdefault('ORIGIN_CI_LOGS_URL', 'http://ci-logs.origin.svc.cluster.local:8080/logs')

os.environ.setdefault('PULSE_FROZEN_DEVELOPMENT_REPOS', '{"shizuha-labs/shizuha":"Origin shizuha-labs/shizuha is frozen (2026-08-15). CLI/harness development SoT is origin.shizuha.com/shizuha-labs/shizuha-beta. Clone, branch, open, and link PRs on shizuha-beta only."}')
os.environ.setdefault('PULSE_OPERATOR_EMAIL', 'operator@example.test')
os.environ.setdefault('PULSE_COS_EMAIL', 'shizuha@shizuha.com')
os.environ.setdefault('PULSE_SUPPRESSED_THERMAL_HOSTS', 'gx10-4,10.200.10.4')

from .settings import *  # noqa: F401,F403,E402

TESTING = True
SOURCE_AUTHORITY_CONTRACT_TEST_BYPASS = True
DEBUG = True
SECRET_KEY = os.environ.get('SECRET_KEY', 'ci-test-secret')
JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', SECRET_KEY)
SHIZUHA_JWT_SIGNING_KEY = os.environ.get('SHIZUHA_JWT_SIGNING_KEY', SECRET_KEY)
# SIMPLE_JWT is built in base settings from ITS SECRET_KEY, so overriding
# SECRET_KEY above does not re-derive the signing key -- it stayed
# 'django-insecure-change-this-in-production'. Any code that fails closed on an
# insecure signing key (publication_handoff._mint_wiki_token) therefore raised
# WIKI_AUTH_UNAVAILABLE under test regardless of what the test set up, so those
# paths could never reach the behaviour they assert. Re-derive it here.
SIMPLE_JWT = {**SIMPLE_JWT, 'SIGNING_KEY': SHIZUHA_JWT_SIGNING_KEY}
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
TEST_RUNNER = 'taskmanager.test_runner.HermeticDiscoverRunner'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('POSTGRES_DB', 'shizuha_pulse_ci'),
        'USER': os.environ.get('POSTGRES_USER', 'postgres'),
        'PASSWORD': os.environ.get('POSTGRES_PASSWORD', 'postgres'),
        'HOST': os.environ.get('POSTGRES_HOST', '127.0.0.1'),
        'PORT': os.environ.get('POSTGRES_PORT', '5432'),
    }
}

# The CI pod starts job-local Redis on 127.0.0.1. This is hermetic: it never
# dials a shared broker, while preserving code paths that intentionally build a
# redis client from CELERY_BROKER_URL.
REDIS_URL = os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/5')
CACHE_REDIS_URL = os.environ.get('CACHE_REDIS_URL', 'redis://127.0.0.1:6379/6')
CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://127.0.0.1:6379/7')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', 'redis://127.0.0.1:6379/7')
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_BROKER_CONNECTION_RETRY = False
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = False
CELERY_BROKER_CONNECTION_MAX_RETRIES = 0
CELERY_BROKER_TRANSPORT_OPTIONS = {'socket_connect_timeout': 1, 'socket_timeout': 1}

# PLS-487 / ADR-0072: hermetic suite stays on LocMem (no live-Redis dependency).
# Production settings resolve to RedisCache; the cross-process shared-backend
# regression spins its own Redis alias and does not use this default.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'pulse-hermetic-test-settings',
    }
}

# PLS-1187: the production lane-health snapshot cache (stale-while-revalidate)
# must stay OFF in the hermetic suite — the existing lane-health/circuit tests
# assert on the live builder path (e.g. the healthy-Redis test requires real
# client reads per request). Cache behaviour is covered by
# tasks/tests/test_pls1187_lane_health_snapshot_cache.py, which opts in via
# override_settings.
PULSE_LANE_HEALTH_SNAPSHOT_CACHE_ENABLED = False

SHIZUHA_ID_API_URL = os.environ.get('SHIZUHA_ID_API_URL_TEST', 'http://127.0.0.1:9/api')
SHIZUHA_CONNECT_API_URL = os.environ.get('SHIZUHA_CONNECT_API_URL_TEST', 'http://127.0.0.1:9/api')
SHIZUHA_ADMIN_API_URL = os.environ.get('SHIZUHA_ADMIN_API_URL_TEST', 'http://127.0.0.1:9/api')
SHIZUHA_AGENT_API_URL = os.environ.get('SHIZUHA_AGENT_API_URL_TEST', '')
WEBHOOK_BASE_URL = os.environ.get('WEBHOOK_BASE_URL', 'http://127.0.0.1:8002')

if os.environ.get('SHIZUHA_TEST_FAULTHANDLER', '1') not in {'0', 'false', 'False'}:
    # Keep this comfortably below the 30-minute CI gate, but above the normal
    # single-process arm64 suite runtime.  The former 300-second watchdog fired
    # during healthy runs as the suite grew; CPython 3.12 then segfaulted while
    # dumping a concurrently-mutating Django query stack (Origin run 8564).
    faulthandler.dump_traceback_later(
        int(os.environ.get('SHIZUHA_TEST_TRACEBACK_AFTER', '900')),
        repeat=True,
    )
