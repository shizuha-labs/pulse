===========
Actions API
===========

The Actions API handles automated actions that may require approval before execution.

.. contents:: On This Page
   :local:
   :depth: 2

Submit Action
-------------

Submit a new action for approval or immediate execution.

.. code-block:: http

    POST /api/actions/submit/

Request Body
^^^^^^^^^^^^

.. code-block:: json

    {
        "title": "Execute Hedging Trade",
        "item_type": "action.hedging.short",
        "source": "arbiter-backend",
        "description": "Short hedge for BTC exposure",
        "requires_approval": true,
        "payload": {
            "symbol": "BTCUSDT",
            "side": "sell",
            "quantity": 0.1,
            "price_limit": 45000
        },
        "labels": {
            "strategy": "momentum",
            "risk_level": "medium"
        },
        "callback_url": "https://api.arbiter.local/execute",
        "callback_method": "POST",
        "callback_headers": {
            "Authorization": "Bearer xxx"
        },
        "due_date": "2024-01-15T18:00:00Z"
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
     - Action title
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
     - Action type (default: ``action.generic``)
   * - ``description``
     - string
     - Detailed description
   * - ``requires_approval``
     - boolean
     - Whether approval needed (default: true)
   * - ``payload``
     - object
     - Action-specific data
   * - ``labels``
     - object
     - Key-value labels
   * - ``priority``
     - string
     - ``low``, ``normal``, ``high``, ``urgent``
   * - ``callback_url``
     - string
     - Webhook URL for execution
   * - ``callback_method``
     - string
     - HTTP method (default: POST)
   * - ``callback_headers``
     - object
     - HTTP headers for callback
   * - ``due_date``
     - datetime
     - Expiration time

Response
^^^^^^^^

.. code-block:: json

    {
        "message": "Action submitted",
        "item_id": 456,
        "status": "pending",
        "requires_approval": true
    }

Action Workflow
---------------

.. mermaid::

    stateDiagram-v2
        [*] --> pending: Submit Action
        pending --> approved: Approve
        pending --> rejected: Reject
        approved --> executing: Execute
        executing --> completed: Success
        executing --> failed: Error
        failed --> executing: Retry
        rejected --> [*]
        completed --> [*]

Approve Action
--------------

Approve a pending action.

.. code-block:: http

    POST /api/items/{id}/approve/

Request Body
^^^^^^^^^^^^

.. code-block:: json

    {
        "comment": "Approved - risk within acceptable limits"
    }

Response
^^^^^^^^

.. code-block:: json

    {
        "message": "Action approved",
        "item_id": 456,
        "status": "approved",
        "approved_by": "user:admin"
    }

Reject Action
-------------

Reject a pending action.

.. code-block:: http

    POST /api/items/{id}/reject/

Request Body
^^^^^^^^^^^^

.. code-block:: json

    {
        "reason": "Market conditions unfavorable"
    }

Response
^^^^^^^^

.. code-block:: json

    {
        "message": "Action rejected",
        "item_id": 456,
        "status": "rejected",
        "rejection_reason": "Market conditions unfavorable"
    }

Execute Action
--------------

Force execute an approved action (if callback configured).

.. code-block:: http

    POST /api/items/{id}/execute/

This triggers the callback webhook if configured.

Response
^^^^^^^^

.. code-block:: json

    {
        "message": "Action executed",
        "item_id": 456,
        "status": "completed",
        "execution_result": {
            "order_id": "12345",
            "filled_quantity": 0.1
        }
    }

Or on failure:

.. code-block:: json

    {
        "message": "Action execution failed",
        "item_id": 456,
        "status": "failed",
        "error_message": "Connection timeout",
        "retry_count": 1
    }

Retry Action
------------

Retry a failed action.

.. code-block:: http

    POST /api/items/{id}/retry/

Response
^^^^^^^^

.. code-block:: json

    {
        "message": "Action retry scheduled",
        "item_id": 456,
        "retry_count": 2,
        "max_retries": 3
    }

Auto-Approval Rules
-------------------

Actions can be automatically approved based on rules:

.. code-block:: bash

    curl -X POST "http://localhost:8010/shizuha-pulse/api/rules/approval/" \
        -H "Content-Type: application/json" \
        -d '{
            "name": "Auto-approve small trades",
            "item_types": ["action.hedging.*"],
            "sources": ["arbiter-backend"],
            "conditions": {
                "and": [
                    {"<=": [{"var": "payload.quantity"}, 0.1]},
                    {"in": [{"var": "payload.symbol"}, ["BTCUSDT", "ETHUSDT"]]}
                ]
            },
            "auto_approve": true,
            "priority": 100
        }'

When a matching action is submitted, it's automatically approved:

.. code-block:: json

    {
        "message": "Action submitted",
        "item_id": 789,
        "status": "approved",
        "requires_approval": false,
        "auto_approved": true
    }

Callback Execution
------------------

When an action with ``callback_url`` is executed:

1. HTTP request sent to ``callback_url``
2. Method from ``callback_method`` (default: POST)
3. Headers from ``callback_headers``
4. Body contains action payload

Request Format
^^^^^^^^^^^^^^

.. code-block:: http

    POST https://api.arbiter.local/execute
    Content-Type: application/json
    Authorization: Bearer xxx

    {
        "action_id": 456,
        "payload": {
            "symbol": "BTCUSDT",
            "side": "sell",
            "quantity": 0.1
        },
        "approved_by": "user:admin",
        "approved_at": "2024-01-15T14:30:00Z"
    }

Expected Response
^^^^^^^^^^^^^^^^^

Success (2xx):

.. code-block:: json

    {
        "order_id": "12345",
        "status": "filled"
    }

This is stored in ``execution_result``.

Failure (4xx/5xx): Error is stored in ``error_message`` and retry may be scheduled.

View Reference
--------------

ActionSubmitView
^^^^^^^^^^^^^^^^

.. autoclass:: tasks.views.ActionSubmitView
   :members:
   :undoc-members:
   :show-inheritance:
