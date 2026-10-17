=========
Rules API
=========

APIs for managing approval, silence, escalation, and inhibition rules.

.. contents:: On This Page
   :local:
   :depth: 2

Approval Rules
--------------

Manage rules that auto-approve or auto-reject actions.

Endpoints
^^^^^^^^^

.. list-table::
   :widths: 10 40 50
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - GET
     - ``/rules/approval/``
     - List all approval rules
   * - POST
     - ``/rules/approval/``
     - Create approval rule
   * - GET
     - ``/rules/approval/{id}/``
     - Get rule details
   * - PUT
     - ``/rules/approval/{id}/``
     - Update rule
   * - DELETE
     - ``/rules/approval/{id}/``
     - Delete rule
   * - POST
     - ``/rules/approval/{id}/test/``
     - Test rule against sample data

Create Rule
^^^^^^^^^^^

.. code-block:: http

    POST /api/rules/approval/

.. code-block:: json

    {
        "name": "Auto-approve small trades",
        "description": "Automatically approve trades under 0.1 BTC",
        "item_types": ["action.hedging.*", "action.trading.*"],
        "sources": ["arbiter-backend"],
        "conditions": {
            "and": [
                {"<=": [{"var": "payload.quantity"}, 0.1]},
                {"in": [{"var": "payload.symbol"}, ["BTCUSDT", "ETHUSDT"]]}
            ]
        },
        "auto_approve": true,
        "auto_reject": false,
        "priority": 100,
        "is_active": true
    }

Conditions Format
^^^^^^^^^^^^^^^^^

Uses JSON Logic format. Available variables:

- ``payload.*`` - Access payload fields
- ``labels.*`` - Access label values
- ``priority`` - Item priority
- ``source`` - Source identifier

Silence Rules
-------------

Manage rules that suppress notifications.

Endpoints
^^^^^^^^^

.. list-table::
   :widths: 10 40 50
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - GET
     - ``/rules/silence/``
     - List all silence rules
   * - POST
     - ``/rules/silence/``
     - Create silence rule
   * - GET
     - ``/rules/silence/{id}/``
     - Get rule details
   * - PUT
     - ``/rules/silence/{id}/``
     - Update rule
   * - DELETE
     - ``/rules/silence/{id}/``
     - Delete rule
   * - GET
     - ``/rules/silence/active/``
     - List active rules
   * - POST
     - ``/rules/silence/{id}/test/``
     - Test rule

Create Rule
^^^^^^^^^^^

.. code-block:: http

    POST /api/rules/silence/

.. code-block:: json

    {
        "name": "Maintenance Window",
        "description": "Suppress alerts during scheduled maintenance",
        "modes": ["alert"],
        "item_types": ["alert.*"],
        "sources": ["prometheus", "grafana"],
        "severities": ["warning", "error"],
        "label_matchers": {
            "environment": "production",
            "team": "platform"
        },
        "starts_at": "2024-01-15T02:00:00Z",
        "ends_at": "2024-01-15T04:00:00Z",
        "reason": "Scheduled database maintenance",
        "is_active": true
    }

Recurring Silence
^^^^^^^^^^^^^^^^^

.. code-block:: json

    {
        "name": "Weekly Maintenance",
        "modes": ["alert"],
        "is_recurring": true,
        "recurrence_pattern": "0 2 * * 0",
        "reason": "Weekly backup window"
    }

Escalation Rules
----------------

Manage rules that escalate unhandled items.

Endpoints
^^^^^^^^^

.. list-table::
   :widths: 10 40 50
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - GET
     - ``/rules/escalation/``
     - List all escalation rules
   * - POST
     - ``/rules/escalation/``
     - Create escalation rule
   * - GET
     - ``/rules/escalation/{id}/``
     - Get rule details
   * - PUT
     - ``/rules/escalation/{id}/``
     - Update rule
   * - DELETE
     - ``/rules/escalation/{id}/``
     - Delete rule

Create Rule
^^^^^^^^^^^

.. code-block:: http

    POST /api/rules/escalation/

.. code-block:: json

    {
        "name": "Critical Production Alerts",
        "description": "Escalate critical production alerts",
        "modes": ["alert"],
        "item_types": ["alert.*"],
        "sources": [],
        "min_severity": "error",
        "label_matchers": {
            "environment": "production"
        },
        "stages": [
            {
                "delay_minutes": 15,
                "actions": {
                    "bump_severity": "critical",
                    "add_channels": ["slack"]
                }
            },
            {
                "delay_minutes": 30,
                "actions": {
                    "add_channels": ["pagerduty"],
                    "set_priority": "urgent"
                }
            },
            {
                "delay_minutes": 60,
                "actions": {
                    "reassign_to": "engineering-lead@company.com",
                    "create_action": {
                        "item_type": "action.escalation",
                        "title": "ESCALATED: {title}"
                    }
                }
            }
        ],
        "is_active": true
    }

Stage Actions
^^^^^^^^^^^^^

Available actions per stage:

- ``bump_severity`` - Increase severity level
- ``add_channels`` - Add notification channels
- ``set_priority`` - Set priority level
- ``reassign_to`` - Reassign to user/email
- ``create_action`` - Create follow-up action

Inhibition Rules
----------------

Manage rules that suppress alerts during cascading failures.

Endpoints
^^^^^^^^^

.. list-table::
   :widths: 10 40 50
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - GET
     - ``/rules/inhibition/``
     - List all inhibition rules
   * - POST
     - ``/rules/inhibition/``
     - Create inhibition rule
   * - GET
     - ``/rules/inhibition/{id}/``
     - Get rule details
   * - PUT
     - ``/rules/inhibition/{id}/``
     - Update rule
   * - DELETE
     - ``/rules/inhibition/{id}/``
     - Delete rule
   * - POST
     - ``/rules/inhibition/{id}/test/``
     - Test rule

Create Rule
^^^^^^^^^^^

.. code-block:: http

    POST /api/rules/inhibition/

.. code-block:: json

    {
        "name": "Database down inhibits API errors",
        "description": "Suppress API timeout alerts when database is down",
        "source_item_types": ["alert.database.*"],
        "source_severities": ["critical"],
        "source_label_matchers": {
            "type": "connection"
        },
        "target_item_types": ["alert.api.*"],
        "target_severities": ["warning", "error"],
        "target_label_matchers": {},
        "equal_labels": ["environment", "cluster"],
        "is_active": true
    }

Equal Labels
^^^^^^^^^^^^

The ``equal_labels`` field specifies labels that must match between source
and target alerts for inhibition to apply.

Testing Rules
-------------

Test Approval Rule
^^^^^^^^^^^^^^^^^^

.. code-block:: http

    POST /api/rules/approval/{id}/test/

.. code-block:: json

    {
        "item_type": "action.hedging.short",
        "source": "arbiter-backend",
        "payload": {
            "symbol": "BTCUSDT",
            "quantity": 0.05
        }
    }

Response:

.. code-block:: json

    {
        "matches": true,
        "action": "auto_approve"
    }

Test Silence Rule
^^^^^^^^^^^^^^^^^

.. code-block:: http

    POST /api/rules/silence/{id}/test/

.. code-block:: json

    {
        "mode": "alert",
        "item_type": "alert.cpu",
        "source": "prometheus",
        "severity": "warning",
        "labels": {
            "environment": "production",
            "team": "platform"
        }
    }

Response:

.. code-block:: json

    {
        "matches": true,
        "is_currently_active": true,
        "expires_at": "2024-01-15T04:00:00Z"
    }

Test Inhibition Rule
^^^^^^^^^^^^^^^^^^^^

.. code-block:: http

    POST /api/rules/inhibition/{id}/test/

.. code-block:: json

    {
        "item_type": "alert.api.timeout",
        "severity": "error",
        "labels": {
            "environment": "production",
            "cluster": "us-east-1"
        }
    }

Response:

.. code-block:: json

    {
        "would_be_inhibited": true,
        "inhibiting_alerts": [
            {
                "id": 123,
                "title": "Database Connection Failed",
                "severity": "critical"
            }
        ]
    }

Supporting Endpoints
--------------------

Subscriptions
^^^^^^^^^^^^^

.. list-table::
   :widths: 10 40 50
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - CRUD
     - ``/subscriptions/``
     - Manage notification subscriptions

Fingerprint Configs
^^^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 10 40 50
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - GET
     - ``/fingerprint-configs/``
     - List configs
   * - POST
     - ``/fingerprint-configs/``
     - Create config
   * - GET
     - ``/fingerprint-configs/{source}/``
     - Get by source
   * - PUT
     - ``/fingerprint-configs/{source}/``
     - Update config
   * - DELETE
     - ``/fingerprint-configs/{source}/``
     - Delete config

Example:

.. code-block:: json

    {
        "source": "prometheus",
        "label_fields": ["alertname", "job", "instance"],
        "payload_fields": [],
        "is_active": true
    }
