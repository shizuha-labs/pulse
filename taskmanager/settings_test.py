from .settings import *

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

INSTALLED_APPS = [app for app in INSTALLED_APPS if app not in (
    'django_celery_beat',
    'rest_framework_simplejwt.token_blacklist',
    'rest_framework_simplejwt',
    'drf_spectacular',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'allauth.socialaccount.providers.github',
    'dj_rest_auth',
    'dj_rest_auth.registration',
    'django.contrib.sites',
)]

# PLAT-8967: the merge-time review gate is ON in production (settings.py
# default) but OFF under the test suite — the pre-existing merge-event
# contract tests across the suite pin the pre-gate advance behavior. The
# gate's own regression class (PLAT8967MergeReviewGateTests) enables it
# explicitly via override_settings.
PULSE_MERGE_REVIEW_GATE_ENABLED = False
