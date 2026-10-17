============
Installation
============

Complete installation guide for Shizuha Pulse.

.. contents:: On This Page
   :local:
   :depth: 2

Docker Installation (Recommended)
---------------------------------

The easiest way to run Shizuha Pulse is with Docker Compose.

Prerequisites
^^^^^^^^^^^^^

- Docker 20.10+
- Docker Compose 2.0+

Quick Start
^^^^^^^^^^^

.. code-block:: bash

    # Clone the repository
    git clone https://github.com/shizuha-stack/shizuha-pulse.git
    cd cli-pulse

    # Start all services
    docker-compose -f docker-compose.dev.yml up --build

This starts:

- **hub** - Main Django application (port 8010)
- **db** - PostgreSQL database (port 5433)
- **redis** - Redis cache/broker (port 6380)
- **celery-worker** - Async task worker
- **celery-beat** - Scheduled task runner

Verify Installation
^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    # Check API is responding
    curl http://localhost:8010/shizuha-pulse/api/items/

    # Check health
    curl http://localhost:8010/shizuha-pulse/api/items/statistics/

Manual Installation
-------------------

For development or custom deployments.

Prerequisites
^^^^^^^^^^^^^

- Python 3.10+
- PostgreSQL 14+
- Redis 7+

Setup Steps
^^^^^^^^^^^

1. **Create virtual environment**:

   .. code-block:: bash

       python -m venv venv
       source venv/bin/activate  # Linux/Mac
       # or: venv\Scripts\activate  # Windows

2. **Install dependencies**:

   .. code-block:: bash

       pip install -r requirements.txt

3. **Configure environment**:

   .. code-block:: bash

       export DJANGO_SETTINGS_MODULE=taskmanager.settings
       export SECRET_KEY=your-secret-key
       export POSTGRES_DB=shizuha_pulse
       export POSTGRES_USER=postgres
       export POSTGRES_PASSWORD=postgres
       export POSTGRES_HOST=localhost
       export POSTGRES_PORT=5432
       export CELERY_BROKER_URL=redis://localhost:6379/0

4. **Run migrations**:

   .. code-block:: bash

       python manage.py migrate

5. **Start the server**:

   .. code-block:: bash

       python manage.py runserver 0.0.0.0:8000

6. **Start Celery worker** (in separate terminal):

   .. code-block:: bash

       celery -A taskmanager worker -l INFO

7. **Start Celery beat** (in separate terminal):

   .. code-block:: bash

       celery -A taskmanager beat -l INFO

Production Deployment
---------------------

For production environments, additional steps are recommended.

Using Gunicorn
^^^^^^^^^^^^^^

.. code-block:: bash

    pip install gunicorn
    gunicorn taskmanager.wsgi:application --bind 0.0.0.0:8000 --workers 4

Nginx Configuration
^^^^^^^^^^^^^^^^^^^

.. code-block:: nginx

    upstream shizuha_pulse {
        server 127.0.0.1:8000;
    }

    server {
        listen 80;
        server_name pulse.example.com;

        location /shizuha-pulse/ {
            proxy_pass http://shizuha_pulse/shizuha-pulse/;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }

        location /shizuha-pulse/static/ {
            alias /app/staticfiles/;
        }
    }

Environment Variables
^^^^^^^^^^^^^^^^^^^^^

Required for production:

.. code-block:: bash

    # Security
    SECRET_KEY=<generate-a-strong-key>
    DEBUG=0
    ALLOWED_HOSTS=pulse.example.com

    # Database
    POSTGRES_DB=shizuha_pulse
    POSTGRES_USER=shizuha
    POSTGRES_PASSWORD=<strong-password>
    POSTGRES_HOST=db.example.com
    POSTGRES_PORT=5432

    # Redis
    CELERY_BROKER_URL=redis://:password@redis.example.com:6379/0
    CELERY_RESULT_BACKEND=redis://:password@redis.example.com:6379/0

    # Optional: Notification service
    NOTIFICATION_API_URL=https://notify.example.com/api
    NOTIFICATION_USERNAME=shizuha
    NOTIFICATION_PASSWORD=<password>

Kubernetes Deployment
---------------------

Example Kubernetes manifests are available in the ``k8s/`` directory.

.. code-block:: bash

    kubectl apply -f k8s/namespace.yaml
    kubectl apply -f k8s/configmap.yaml
    kubectl apply -f k8s/secrets.yaml
    kubectl apply -f k8s/deployment.yaml
    kubectl apply -f k8s/service.yaml

Troubleshooting
---------------

Database Connection Failed
^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    # Check PostgreSQL is running
    docker-compose ps db

    # Check connection
    psql -h localhost -p 5433 -U postgres -d shizuha_pulse

Redis Connection Failed
^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    # Check Redis is running
    docker-compose ps redis

    # Test connection
    redis-cli -h localhost -p 6380 ping

Migrations Failed
^^^^^^^^^^^^^^^^^

.. code-block:: bash

    # Reset migrations (development only!)
    docker-compose exec hub python manage.py migrate --fake-initial

    # Or recreate database
    docker-compose down -v
    docker-compose up --build
