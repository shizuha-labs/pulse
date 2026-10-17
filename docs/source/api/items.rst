=========
Items API
=========

The Items API provides unified CRUD operations for tasks, actions, and alerts.

.. contents:: On This Page
   :local:
   :depth: 2

List Items
----------

.. code-block:: http

    GET /api/items/

Query Parameters
^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Parameter
     - Type
     - Description
   * - ``mode``
     - string
     - Filter by mode: ``task``, ``action``, ``alert``
   * - ``status``
     - string
     - Filter by status
   * - ``priority``
     - string
     - Filter by priority
   * - ``severity``
     - string
     - Filter by severity (alerts)
   * - ``item_type``
     - string
     - Filter by type (supports ``*`` wildcard)
   * - ``source``
     - string
     - Filter by source
   * - ``assignee_id``
     - integer
     - Filter by assignee
   * - ``due_after``
     - datetime
     - Items due after date
   * - ``due_before``
     - datetime
     - Items due before date
   * - ``tags``
     - string
     - Filter by tag (comma-separated)
   * - ``is_recurring``
     - boolean
     - Filter recurring items
   * - ``page``
     - integer
     - Page number (default: 1)
   * - ``page_size``
     - integer
     - Items per page (default: 20)

Example Request
^^^^^^^^^^^^^^^

.. code-block:: bash

    curl -X GET "http://localhost:8010/shizuha-pulse/api/items/?mode=alert&status=firing&severity=critical"

Example Response
^^^^^^^^^^^^^^^^

.. code-block:: json

    {
        "count": 2,
        "next": null,
        "previous": null,
        "results": [
            {
                "id": 1,
                "mode": "alert",
                "item_type": "alert.cpu",
                "title": "High CPU Usage",
                "status": "firing",
                "severity": "critical",
                "labels": {"host": "web-01"},
                "created_at": "2024-01-15T10:30:00Z"
            }
        ]
    }

Create Item
-----------

.. code-block:: http

    POST /api/items/

Request Body
^^^^^^^^^^^^

.. tabs::

    .. tab:: Task

        .. code-block:: json

            {
                "mode": "task",
                "item_type": "task.reminder",
                "title": "Review quarterly report",
                "description": "Q4 2024 financial review",
                "due_date": "2024-01-20T17:00:00Z",
                "priority": "high",
                "tags": "work,finance"
            }

    .. tab:: Action

        .. code-block:: json

            {
                "mode": "action",
                "item_type": "action.hedging.short",
                "title": "Execute hedging trade",
                "source": "arbiter-backend",
                "requires_approval": true,
                "payload": {
                    "symbol": "BTCUSDT",
                    "side": "sell",
                    "quantity": 0.1
                },
                "callback_url": "https://api.example.com/execute"
            }

    .. tab:: Alert

        .. code-block:: json

            {
                "mode": "alert",
                "item_type": "alert.cpu",
                "title": "High CPU Usage",
                "source": "prometheus",
                "severity": "warning",
                "labels": {
                    "host": "web-01",
                    "job": "node_exporter"
                }
            }

Response
^^^^^^^^

.. code-block:: json

    {
        "id": 123,
        "mode": "task",
        "item_type": "task.reminder",
        "title": "Review quarterly report",
        "status": "pending",
        "created_at": "2024-01-15T10:30:00Z"
    }

Get Item
--------

.. code-block:: http

    GET /api/items/{id}/

Response
^^^^^^^^

Returns full item details including all fields.

Update Item
-----------

.. code-block:: http

    PUT /api/items/{id}/
    PATCH /api/items/{id}/

Request Body
^^^^^^^^^^^^

Include only fields to update:

.. code-block:: json

    {
        "title": "Updated title",
        "priority": "urgent",
        "due_date": "2024-01-25T17:00:00Z"
    }

Delete Item
-----------

.. code-block:: http

    DELETE /api/items/{id}/

Response: ``204 No Content``

Filtered Endpoints
------------------

Tasks Only
^^^^^^^^^^

.. code-block:: http

    GET /api/items/tasks/

Returns items with ``mode=task``.

Actions Only
^^^^^^^^^^^^

.. code-block:: http

    GET /api/items/actions/

Returns items with ``mode=action``.

Alerts Only
^^^^^^^^^^^

.. code-block:: http

    GET /api/items/alerts/

Returns items with ``mode=alert`` and ``status`` in ``[firing, acknowledged]``.

Query Parameters
""""""""""""""""

- ``status`` - Include resolved alerts

Pending Items
^^^^^^^^^^^^^

.. code-block:: http

    GET /api/items/pending/

Returns grouped pending items:

.. code-block:: json

    {
        "tasks": [...],
        "actions": [...],
        "alerts": [...]
    }

Overdue Items
^^^^^^^^^^^^^

.. code-block:: http

    GET /api/items/overdue/

Returns items past their due date.

Due Soon
^^^^^^^^

.. code-block:: http

    GET /api/items/due_soon/?hours=24

Returns items due within specified hours.

Item Actions
------------

Complete Task
^^^^^^^^^^^^^

.. code-block:: http

    POST /api/items/{id}/complete/

Response
""""""""

.. code-block:: json

    {
        "message": "Task completed",
        "task_id": 123
    }

Approve Action
^^^^^^^^^^^^^^

.. code-block:: http

    POST /api/items/{id}/approve/

Request Body
""""""""""""

.. code-block:: json

    {
        "comment": "Approved for execution"
    }

Reject Action
^^^^^^^^^^^^^

.. code-block:: http

    POST /api/items/{id}/reject/

Request Body
""""""""""""

.. code-block:: json

    {
        "reason": "Risk too high for current market conditions"
    }

Acknowledge Alert
^^^^^^^^^^^^^^^^^

.. code-block:: http

    POST /api/items/{id}/acknowledge/

Request Body
""""""""""""

.. code-block:: json

    {
        "comment": "Investigating issue"
    }

Resolve Alert
^^^^^^^^^^^^^

.. code-block:: http

    POST /api/items/{id}/resolve/

Request Body
""""""""""""

.. code-block:: json

    {
        "note": "Root cause identified and fixed"
    }

Silence Item
^^^^^^^^^^^^

.. code-block:: http

    POST /api/items/{id}/silence/

Request Body
""""""""""""

.. code-block:: json

    {
        "duration_minutes": 60,
        "reason": "Maintenance in progress"
    }

Assign Item
^^^^^^^^^^^

.. code-block:: http

    POST /api/items/{id}/assign/

Request Body
""""""""""""

.. code-block:: json

    {
        "assignee_id": 42,
        "assignee_email": "user@example.com"
    }

Statistics
----------

.. code-block:: http

    GET /api/items/statistics/

Response
^^^^^^^^

.. code-block:: json

    {
        "total_items": 150,
        "by_mode": {
            "task": 80,
            "action": 30,
            "alert": 40
        },
        "by_status": {
            "pending": 45,
            "in_progress": 20,
            "completed": 50,
            "firing": 15,
            "resolved": 20
        },
        "by_priority": {
            "low": 30,
            "normal": 80,
            "high": 30,
            "urgent": 10
        }
    }

ViewSet Reference
-----------------

.. autoclass:: tasks.views.ItemViewSet
   :members:
   :undoc-members:
   :show-inheritance:
   :exclude-members: permission_classes, queryset, serializer_class
