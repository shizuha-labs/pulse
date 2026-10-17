==========
Quickstart
==========

Get started with Shizuha Hub in 5 minutes.

.. contents:: On This Page
   :local:
   :depth: 2

Prerequisites
-------------

- Docker and Docker Compose
- curl or Postman (for API testing)

Start the Service
-----------------

1. Clone and start:

   .. code-block:: bash

       cd cli-pulse
       docker-compose -f docker-compose.dev.yml up --build

2. Wait for services to be ready (about 30 seconds)

3. The API is available at: ``http://localhost:8010/shizuha-pulse/api/``

Create Your First Task
----------------------

.. code-block:: bash

    curl -X POST "http://localhost:8010/shizuha-pulse/api/items/" \
        -H "Content-Type: application/json" \
        -d '{
            "mode": "task",
            "title": "Read the documentation",
            "priority": "high",
            "due_date": "2024-01-20T17:00:00Z"
        }'

Response:

.. code-block:: json

    {
        "id": 1,
        "mode": "task",
        "title": "Read the documentation",
        "status": "pending",
        "priority": "high"
    }

Submit an Alert
---------------

.. code-block:: bash

    curl -X POST "http://localhost:8010/shizuha-pulse/api/alerts/" \
        -H "Content-Type: application/json" \
        -d '{
            "title": "High CPU Usage",
            "source": "prometheus",
            "severity": "warning",
            "labels": {
                "host": "web-01",
                "job": "node_exporter"
            }
        }'

Response:

.. code-block:: json

    {
        "message": "Alert created",
        "item_id": 2,
        "fingerprint": "abc123...",
        "is_new": true
    }

Acknowledge the Alert
---------------------

.. code-block:: bash

    curl -X POST "http://localhost:8010/shizuha-pulse/api/items/2/acknowledge/" \
        -H "Content-Type: application/json" \
        -d '{"comment": "Investigating"}'

Submit an Action
----------------

.. code-block:: bash

    curl -X POST "http://localhost:8010/shizuha-pulse/api/actions/submit/" \
        -H "Content-Type: application/json" \
        -d '{
            "title": "Scale up web servers",
            "source": "auto-scaler",
            "requires_approval": true,
            "payload": {
                "service": "web",
                "current_replicas": 3,
                "target_replicas": 5
            }
        }'

Approve the Action
------------------

.. code-block:: bash

    curl -X POST "http://localhost:8010/shizuha-pulse/api/items/3/approve/" \
        -H "Content-Type: application/json" \
        -d '{"comment": "Approved for scaling"}'

View All Items
--------------

.. code-block:: bash

    curl "http://localhost:8010/shizuha-pulse/api/items/"

View Statistics
---------------

.. code-block:: bash

    curl "http://localhost:8010/shizuha-pulse/api/items/statistics/"

Response:

.. code-block:: json

    {
        "total_items": 3,
        "by_mode": {
            "task": 1,
            "action": 1,
            "alert": 1
        },
        "by_status": {
            "pending": 1,
            "approved": 1,
            "acknowledged": 1
        }
    }

Create a Silence Rule
---------------------

Silence alerts during maintenance:

.. code-block:: bash

    curl -X POST "http://localhost:8010/shizuha-pulse/api/rules/silence/" \
        -H "Content-Type: application/json" \
        -d '{
            "name": "Maintenance Window",
            "modes": ["alert"],
            "label_matchers": {"host": "web-01"},
            "starts_at": "2024-01-15T02:00:00Z",
            "ends_at": "2024-01-15T04:00:00Z"
        }'

Next Steps
----------

- :doc:`installation` - Full installation guide
- :doc:`configuration` - Configuration options
- :doc:`../api/index` - Complete API reference
- :doc:`../models/index` - Data model documentation
