=================
Docker Deployment
=================

Complete Docker deployment guide for Shizuha Pulse.

.. contents:: On This Page
   :local:
   :depth: 2

Development Setup
-----------------

Quick Start
^^^^^^^^^^^

.. code-block:: bash

    # Start all services
    docker-compose -f docker-compose.dev.yml up --build

    # Start in background
    docker-compose -f docker-compose.dev.yml up -d --build

    # View logs
    docker-compose -f docker-compose.dev.yml logs -f hub

    # Stop services
    docker-compose -f docker-compose.dev.yml down

Services Overview
^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Service
     - Port
     - Description
   * - ``hub``
     - 8010
     - Main Django application
   * - ``db``
     - 5433
     - PostgreSQL database
   * - ``redis``
     - 6380
     - Redis cache/broker
   * - ``celery-worker``
     - -
     - Async task processing
   * - ``celery-beat``
     - -
     - Scheduled task execution

Accessing Services
^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    # Django shell
    docker-compose exec hub python manage.py shell

    # Database shell
    docker-compose exec db psql -U postgres -d shizuha_pulse

    # Redis CLI
    docker-compose exec redis redis-cli

    # Run migrations
    docker-compose exec hub python manage.py migrate

    # Create superuser
    docker-compose exec hub python manage.py createsuperuser

Production Setup
----------------

Docker Compose (Production)
^^^^^^^^^^^^^^^^^^^^^^^^^^^

Create ``docker-compose.prod.yml``:

.. code-block:: yaml

    version: '3.8'

    services:
      hub:
        image: shizuha/shizuha-pulse:latest
        restart: always
        environment:
          - DEBUG=0
          - SECRET_KEY=${SECRET_KEY}
          - POSTGRES_DB=${POSTGRES_DB}
          - POSTGRES_USER=${POSTGRES_USER}
          - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
          - POSTGRES_HOST=db
          - CELERY_BROKER_URL=redis://redis:6379/0
        depends_on:
          - db
          - redis
        networks:
          - shizuha

      celery-worker:
        image: shizuha/shizuha-pulse:latest
        command: celery -A taskmanager worker -l INFO
        restart: always
        environment:
          - DEBUG=0
          - SECRET_KEY=${SECRET_KEY}
          - POSTGRES_HOST=db
          - CELERY_BROKER_URL=redis://redis:6379/0
        depends_on:
          - db
          - redis
        networks:
          - shizuha

      celery-beat:
        image: shizuha/shizuha-pulse:latest
        command: celery -A taskmanager beat -l INFO --scheduler django_celery_beat.schedulers:DatabaseScheduler
        restart: always
        environment:
          - DEBUG=0
          - SECRET_KEY=${SECRET_KEY}
          - POSTGRES_HOST=db
          - CELERY_BROKER_URL=redis://redis:6379/0
        depends_on:
          - celery-worker
        networks:
          - shizuha

      db:
        image: postgres:14-alpine
        restart: always
        environment:
          - POSTGRES_DB=${POSTGRES_DB}
          - POSTGRES_USER=${POSTGRES_USER}
          - POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
        volumes:
          - postgres_data:/var/lib/postgresql/data
        networks:
          - shizuha

      redis:
        image: redis:7-alpine
        restart: always
        volumes:
          - redis_data:/data
        networks:
          - shizuha

      nginx:
        image: nginx:alpine
        restart: always
        ports:
          - "80:80"
          - "443:443"
        volumes:
          - ./nginx.conf:/etc/nginx/nginx.conf:ro
          - ./ssl:/etc/nginx/ssl:ro
          - static_files:/app/staticfiles:ro
        depends_on:
          - hub
        networks:
          - shizuha

    volumes:
      postgres_data:
      redis_data:
      static_files:

    networks:
      shizuha:
        driver: bridge

Environment File
^^^^^^^^^^^^^^^^

Create ``.env.prod``:

.. code-block:: bash

    SECRET_KEY=your-super-secret-key-here
    POSTGRES_DB=shizuha_pulse
    POSTGRES_USER=shizuha
    POSTGRES_PASSWORD=super-secure-password

Deploy:

.. code-block:: bash

    docker-compose -f docker-compose.prod.yml --env-file .env.prod up -d

Building Custom Images
----------------------

Dockerfile
^^^^^^^^^^

.. code-block:: dockerfile

    FROM python:3.10-slim

    WORKDIR /app

    # Install system dependencies
    RUN apt-get update && apt-get install -y \
        netcat-traditional \
        && rm -rf /var/lib/apt/lists/*

    # Install Python dependencies
    COPY requirements.txt .
    RUN pip install --no-cache-dir -r requirements.txt

    # Copy application
    COPY . .

    # Collect static files
    RUN python manage.py collectstatic --noinput

    EXPOSE 8000

    CMD ["gunicorn", "taskmanager.wsgi:application", "--bind", "0.0.0.0:8000"]

Build and Push
^^^^^^^^^^^^^^

.. code-block:: bash

    # Build image
    docker build -t shizuha/shizuha-pulse:latest .

    # Tag for registry
    docker tag shizuha/shizuha-pulse:latest registry.example.com/shizuha-pulse:latest

    # Push to registry
    docker push registry.example.com/shizuha-pulse:latest

Health Checks
-------------

Container Health
^^^^^^^^^^^^^^^^

.. code-block:: yaml

    services:
      hub:
        healthcheck:
          test: ["CMD", "curl", "-f", "http://localhost:8000/shizuha-pulse/api/items/statistics/"]
          interval: 30s
          timeout: 10s
          retries: 3
          start_period: 40s

      db:
        healthcheck:
          test: ["CMD-SHELL", "pg_isready -U postgres"]
          interval: 5s
          timeout: 5s
          retries: 5

      redis:
        healthcheck:
          test: ["CMD", "redis-cli", "ping"]
          interval: 5s
          timeout: 5s
          retries: 5

Scaling
-------

Horizontal Scaling
^^^^^^^^^^^^^^^^^^

Scale workers:

.. code-block:: bash

    # Scale Celery workers
    docker-compose up -d --scale celery-worker=3

    # Scale hub (requires load balancer)
    docker-compose up -d --scale hub=2

Monitoring
----------

Container Logs
^^^^^^^^^^^^^^

.. code-block:: bash

    # All services
    docker-compose logs -f

    # Specific service
    docker-compose logs -f hub

    # With timestamps
    docker-compose logs -f -t hub

Resource Monitoring
^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    # Container stats
    docker stats

    # Specific containers
    docker stats shizuha-pulse shizuha-pulse-worker

Backup and Restore
------------------

Database Backup
^^^^^^^^^^^^^^^

.. code-block:: bash

    # Backup
    docker-compose exec db pg_dump -U postgres shizuha_pulse > backup.sql

    # Restore
    docker-compose exec -T db psql -U postgres shizuha_pulse < backup.sql

Volume Backup
^^^^^^^^^^^^^

.. code-block:: bash

    # Backup volumes
    docker run --rm -v shizuha-pulse_postgres_data:/data -v $(pwd):/backup alpine tar czf /backup/postgres_backup.tar.gz -C /data .

    # Restore volumes
    docker run --rm -v shizuha-pulse_postgres_data:/data -v $(pwd):/backup alpine tar xzf /backup/postgres_backup.tar.gz -C /data
