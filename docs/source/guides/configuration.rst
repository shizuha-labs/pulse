=============
Configuration
=============

Configuration options for Shizuha Pulse.

.. contents:: On This Page
   :local:
   :depth: 2

Environment Variables
---------------------

Core Settings
^^^^^^^^^^^^^

.. list-table::
   :widths: 30 20 50
   :header-rows: 1

   * - Variable
     - Default
     - Description
   * - ``SECRET_KEY``
     - (required)
     - Django secret key
   * - ``DEBUG``
     - ``0``
     - Enable debug mode (1/0)
   * - ``ALLOWED_HOSTS``
     - ``*``
     - Comma-separated allowed hosts
   * - ``DJANGO_SETTINGS_MODULE``
     - ``taskmanager.settings``
     - Settings module path

Database Settings
^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 30 20 50
   :header-rows: 1

   * - Variable
     - Default
     - Description
   * - ``POSTGRES_DB``
     - ``shizuha_pulse``
     - Database name
   * - ``POSTGRES_USER``
     - ``postgres``
     - Database user
   * - ``POSTGRES_PASSWORD``
     - (required)
     - Database password
   * - ``POSTGRES_HOST``
     - ``localhost``
     - Database host
   * - ``POSTGRES_PORT``
     - ``5432``
     - Database port

Redis/Celery Settings
^^^^^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 30 20 50
   :header-rows: 1

   * - Variable
     - Default
     - Description
   * - ``CELERY_BROKER_URL``
     - ``redis://localhost:6379/0``
     - Celery broker URL
   * - ``CELERY_RESULT_BACKEND``
     - ``redis://localhost:6379/0``
     - Celery result backend

Notification Settings
^^^^^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 30 20 50
   :header-rows: 1

   * - Variable
     - Default
     - Description
   * - ``NOTIFICATION_API_URL``
     - (optional)
     - Shizuha Notify API URL
   * - ``NOTIFICATION_USERNAME``
     - (optional)
     - Notification service username
   * - ``NOTIFICATION_PASSWORD``
     - (optional)
     - Notification service password
   * - ``AUTO_SEND_REMINDER``
     - ``false``
     - Auto-send task reminders

Django Settings
---------------

Key settings in ``taskmanager/settings.py``:

REST Framework
^^^^^^^^^^^^^^

.. code-block:: python

    REST_FRAMEWORK = {
        'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
        'PAGE_SIZE': 20,
        'DEFAULT_PERMISSION_CLASSES': [
            'rest_framework.permissions.AllowAny',  # Change for production!
        ],
        'DEFAULT_AUTHENTICATION_CLASSES': [
            # Add authentication classes here
        ],
    }

CORS Settings
^^^^^^^^^^^^^

.. code-block:: python

    CORS_ALLOWED_ORIGINS = [
        "http://localhost:3000",
        "http://localhost:8080",
    ]

    # Or allow all (development only)
    CORS_ALLOW_ALL_ORIGINS = True

Celery Configuration
^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
    CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://localhost:6379/0')
    CELERY_ACCEPT_CONTENT = ['json']
    CELERY_TASK_SERIALIZER = 'json'
    CELERY_RESULT_SERIALIZER = 'json'
    CELERY_TIMEZONE = 'UTC'

    # Task routing
    CELERY_TASK_ROUTES = {
        'tasks.tasks.*': {'queue': 'taskmanager'},
    }

Logging
^^^^^^^

.. code-block:: python

    LOGGING = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'verbose': {
                'format': '{levelname} {asctime} {module} {message}',
                'style': '{',
            },
        },
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
                'formatter': 'verbose',
            },
        },
        'loggers': {
            'tasks': {
                'handlers': ['console'],
                'level': 'INFO',
            },
            'tasks.services': {
                'handlers': ['console'],
                'level': 'DEBUG',
            },
        },
    }

Scheduled Tasks
---------------

Configure periodic tasks via Celery Beat:

.. code-block:: python

    from celery.schedules import crontab

    CELERY_BEAT_SCHEDULE = {
        # Process escalations every 5 minutes
        'process-escalations': {
            'task': 'tasks.tasks.process_escalations',
            'schedule': crontab(minute='*/5'),
        },
        # Cleanup expired silences every hour
        'cleanup-silences': {
            'task': 'tasks.tasks.cleanup_expired_silences',
            'schedule': crontab(minute=0),
        },
        # Auto-resolve expired alerts daily
        'auto-resolve-alerts': {
            'task': 'tasks.tasks.auto_resolve_expired',
            'schedule': crontab(hour=0, minute=0),
        },
    }

Or configure via Django Admin:

1. Access admin at ``/shizuha-pulse/admin/``
2. Go to "Periodic Tasks"
3. Create new periodic task with schedule

Feature Flags
-------------

Enable/disable features via settings:

.. code-block:: python

    # Deduplication
    ALERT_DEDUPLICATION_ENABLED = True

    # Escalation
    ESCALATION_ENABLED = True
    ESCALATION_CHECK_INTERVAL = 300  # seconds

    # Silencing
    SILENCING_ENABLED = True

    # Inhibition
    INHIBITION_ENABLED = True

    # Notifications
    NOTIFICATIONS_ENABLED = True

Performance Tuning
------------------

Database
^^^^^^^^

.. code-block:: python

    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('POSTGRES_DB'),
            'USER': os.getenv('POSTGRES_USER'),
            'PASSWORD': os.getenv('POSTGRES_PASSWORD'),
            'HOST': os.getenv('POSTGRES_HOST'),
            'PORT': os.getenv('POSTGRES_PORT'),
            'CONN_MAX_AGE': 60,  # Connection pooling
            'OPTIONS': {
                'connect_timeout': 10,
            },
        }
    }

Caching
^^^^^^^

.. code-block:: python

    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': os.getenv('CELERY_BROKER_URL'),
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            }
        }
    }

    # Cache fingerprint configs
    FINGERPRINT_CONFIG_CACHE_TTL = 300  # 5 minutes
