==========
Alerts API
==========

The Alerts API provides specialized endpoints for alert submission with automatic
deduplication and bulk operations.

.. contents:: On This Page
   :local:
   :depth: 2

Submit Alert
------------

Submit a new alert with automatic fingerprint-based deduplication.

.. code-block:: http

    POST /api/alerts/

Request Body
^^^^^^^^^^^^

.. code-block:: json

    {
        "title": "High CPU Usage",
        "item_type": "alert.cpu",
        "source": "prometheus",
        "severity": "warning",
        "labels": {
            "host": "web-01",
            "job": "node_exporter",
            "instance": "10.0.0.1:9100"
        },
        "payload": {
            "value": 95.5,
            "threshold": 90,
            "duration": "5m"
        },
        "correlation_id": "cpu-check-web-01",
        "description": "CPU usage exceeded 90% for 5 minutes"
    }

Required Fields
^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``title``
     - string
     - Alert title
   * - ``source``
     - string
     - Source system identifier

Optional Fields
^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``item_type``
     - string
     - Alert type (default: ``alert.generic``)
   * - ``severity``
     - string
     - ``info``, ``warning``, ``error``, ``critical``
   * - ``labels``
     - object
     - Key-value labels for matching/filtering
   * - ``payload``
     - object
     - Additional alert data
   * - ``correlation_id``
     - string
     - ID for grouping related alerts
   * - ``description``
     - string
     - Detailed description
   * - ``due_date``
     - datetime
     - Auto-resolve time

Response (New Alert)
^^^^^^^^^^^^^^^^^^^^

.. code-block:: json

    {
        "message": "Alert created",
        "item_id": 123,
        "fingerprint": "8d106e7586e16e9372ffe874ee958612",
        "is_new": true,
        "occurrence_count": 1
    }

Response (Deduplicated)
^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: json

    {
        "message": "Alert deduplicated",
        "item_id": 123,
        "fingerprint": "8d106e7586e16e9372ffe874ee958612",
        "is_new": false,
        "occurrence_count": 5
    }

Example
^^^^^^^

.. code-block:: bash

    curl -X POST "http://localhost:8010/shizuha-pulse/api/alerts/" \
        -H "Content-Type: application/json" \
        -d '{
            "title": "Database Connection Pool Exhausted",
            "source": "app-monitor",
            "severity": "error",
            "labels": {
                "database": "postgres-main",
                "application": "user-service"
            }
        }'

Bulk Acknowledge
----------------

Acknowledge multiple alerts at once.

.. code-block:: http

    POST /api/alerts/bulk-acknowledge/

Request Body
^^^^^^^^^^^^

By Item IDs
"""""""""""

.. code-block:: json

    {
        "item_ids": [1, 2, 3, 4, 5],
        "acknowledged_by": "user:oncall"
    }

By Filters
""""""""""

.. code-block:: json

    {
        "source": "prometheus",
        "severity": "warning",
        "label_matchers": {
            "environment": "production"
        },
        "acknowledged_by": "user:oncall"
    }

Response
^^^^^^^^

.. code-block:: json

    {
        "message": "Acknowledged 5 alerts",
        "count": 5
    }

Bulk Resolve
------------

Resolve multiple alerts at once.

.. code-block:: http

    POST /api/alerts/bulk-resolve/

Request Body
^^^^^^^^^^^^

.. code-block:: json

    {
        "item_ids": [1, 2, 3],
        "resolved_by": "user:oncall",
        "resolution_note": "Issue resolved after deployment fix"
    }

Or by source:

.. code-block:: json

    {
        "source": "prometheus",
        "resolved_by": "auto:deployment",
        "resolution_note": "Automatically resolved after successful deployment"
    }

Response
^^^^^^^^

.. code-block:: json

    {
        "message": "Resolved 3 alerts",
        "count": 3
    }

Deduplication
-------------

How It Works
^^^^^^^^^^^^

1. **Fingerprint Computation**: A hash is computed from:

   - ``item_type``
   - ``source``
   - Configured label fields (or all labels if not configured)
   - Configured payload fields (if any)

2. **Lookup**: Check for existing active alert with same fingerprint

3. **Deduplication**: If found, increment occurrence count and update timestamps

4. **Creation**: If not found, create new alert with fingerprint

Fingerprint Configuration
^^^^^^^^^^^^^^^^^^^^^^^^^

Configure which fields to use for fingerprinting per source:

.. code-block:: bash

    curl -X POST "http://localhost:8010/shizuha-pulse/api/fingerprint-configs/" \
        -H "Content-Type: application/json" \
        -d '{
            "source": "prometheus",
            "label_fields": ["alertname", "job", "instance"],
            "payload_fields": []
        }'

Example Scenario
^^^^^^^^^^^^^^^^

.. code-block:: python

    # First submission
    POST /api/alerts/
    {"title": "High CPU", "source": "prometheus", "labels": {"host": "web-01"}}
    # Response: is_new=true, occurrence_count=1

    # Same alert 5 minutes later
    POST /api/alerts/
    {"title": "High CPU", "source": "prometheus", "labels": {"host": "web-01"}}
    # Response: is_new=false, occurrence_count=2

    # After resolution
    POST /api/items/123/resolve/

    # Same alert again (creates new since previous resolved)
    POST /api/alerts/
    {"title": "High CPU", "source": "prometheus", "labels": {"host": "web-01"}}
    # Response: is_new=true, occurrence_count=1

Recovery Alerts
---------------

Recovery alerts can automatically resolve matching firing alerts.

Via Correlation ID
^^^^^^^^^^^^^^^^^^

If the original alert has a ``correlation_id``, a recovery can reference it:

.. code-block:: json

    {
        "title": "CPU Usage Recovered",
        "source": "prometheus",
        "severity": "info",
        "correlation_id": "cpu-check-web-01",
        "is_recovery": true
    }

This automatically resolves all firing alerts with the same ``correlation_id``.

View Reference
--------------

AlertSubmitView
^^^^^^^^^^^^^^^

.. autoclass:: tasks.views.AlertSubmitView
   :members:
   :undoc-members:
   :show-inheritance:

AlertBulkAcknowledgeView
^^^^^^^^^^^^^^^^^^^^^^^^

.. autoclass:: tasks.views.AlertBulkAcknowledgeView
   :members:
   :undoc-members:
   :show-inheritance:

AlertBulkResolveView
^^^^^^^^^^^^^^^^^^^^

.. autoclass:: tasks.views.AlertBulkResolveView
   :members:
   :undoc-members:
   :show-inheritance:
