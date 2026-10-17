===================
Inhibition Service
===================

The ``InhibitionService`` evaluates inhibition rules to suppress "child" alerts when
"parent" alerts are firing, reducing noise during cascading failures.

.. contents:: On This Page
   :local:
   :depth: 2

Overview
--------

Inhibition is useful when:

1. One failure causes multiple related alerts
2. You want to see the root cause, not symptoms
3. Reducing alert fatigue during incidents

.. mermaid::

    flowchart TD
        subgraph "Source Alert"
            SA[Database Down<br/>severity: critical]
        end

        subgraph "Target Alerts (Inhibited)"
            TA1[API Timeout]
            TA2[Query Slow]
            TA3[Connection Failed]
        end

        SA -->|inhibits| TA1
        SA -->|inhibits| TA2
        SA -->|inhibits| TA3

API Reference
-------------

.. autoclass:: tasks.services.inhibition.InhibitionService
   :members:
   :undoc-members:
   :show-inheritance:

Key Methods
-----------

is_inhibited
^^^^^^^^^^^^

Checks if an alert is inhibited by any firing source alert.

.. code-block:: python

    from tasks.services import InhibitionService
    from tasks.models import Item

    alert = Item.objects.get(id=123)

    if InhibitionService.is_inhibited(alert):
        print("Alert is inhibited by a parent alert")
        # Don't send notifications for this alert
    else:
        send_notification(alert)

get_inhibiting_alerts
^^^^^^^^^^^^^^^^^^^^^

Returns the list of alerts that are inhibiting this alert.

.. code-block:: python

    inhibitors = InhibitionService.get_inhibiting_alerts(alert)

    for inhibitor in inhibitors:
        print(f"Inhibited by: {inhibitor.title} ({inhibitor.severity})")

get_inhibited_alerts
^^^^^^^^^^^^^^^^^^^^

Returns alerts that would be inhibited by a source alert.

.. code-block:: python

    # Get all alerts that this source alert inhibits
    inhibited = InhibitionService.get_inhibited_alerts(source_alert)

    print(f"This alert inhibits {len(inhibited)} other alerts")

Inhibition Rules
----------------

Rule Structure
^^^^^^^^^^^^^^

.. code-block:: python

    from tasks.models import InhibitionRule

    rule = InhibitionRule.objects.create(
        name='Database down inhibits API errors',

        # Source criteria (the "parent" alert)
        source_item_types=['alert.database.*'],
        source_severities=['critical'],
        source_label_matchers={'type': 'connection'},

        # Target criteria (alerts to inhibit)
        target_item_types=['alert.api.*'],
        target_severities=['warning', 'error'],
        target_label_matchers={},

        # Labels that must match between source and target
        equal_labels=['environment', 'cluster']
    )

Equal Labels
^^^^^^^^^^^^

The ``equal_labels`` field specifies labels that must have matching values
between the source and target alerts for inhibition to apply.

.. code-block:: python

    # Only inhibit if environment and cluster match
    rule = InhibitionRule.objects.create(
        name='Same-cluster inhibition',
        source_item_types=['alert.database.*'],
        target_item_types=['alert.api.*'],
        equal_labels=['environment', 'cluster']
    )

    # Source alert: environment=prod, cluster=us-east-1
    # Target alert: environment=prod, cluster=us-east-1  -> INHIBITED
    # Target alert: environment=prod, cluster=us-west-2  -> NOT inhibited

Common Patterns
---------------

Database Cascading
^^^^^^^^^^^^^^^^^^

.. code-block:: python

    # Primary database failure inhibits replica and connection alerts
    InhibitionRule.objects.create(
        name='Primary DB inhibits replicas',
        source_item_types=['alert.database.primary.*'],
        source_severities=['critical'],
        target_item_types=[
            'alert.database.replica.*',
            'alert.database.connection.*',
            'alert.api.timeout'
        ],
        equal_labels=['cluster']
    )

Network Cascading
^^^^^^^^^^^^^^^^^

.. code-block:: python

    # Network partition inhibits all service alerts in affected zone
    InhibitionRule.objects.create(
        name='Network partition cascade',
        source_item_types=['alert.network.partition'],
        source_severities=['critical'],
        target_item_types=['alert.service.*'],
        equal_labels=['availability_zone']
    )

Service Dependencies
^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    # Auth service down inhibits downstream service alerts
    InhibitionRule.objects.create(
        name='Auth service cascade',
        source_item_types=['alert.service.auth.*'],
        source_severities=['critical', 'error'],
        target_item_types=[
            'alert.service.api.*',
            'alert.service.web.*',
            'alert.service.mobile.*'
        ],
        source_label_matchers={'component': 'authentication'}
    )

Severity Hierarchy
------------------

Inhibition typically flows from higher to lower severity:

.. code-block:: python

    # Critical alerts inhibit lower severity related alerts
    InhibitionRule.objects.create(
        name='Severity cascade',
        source_severities=['critical'],
        target_severities=['warning', 'error'],
        equal_labels=['service', 'environment']
    )

Processing Order
----------------

When processing a new alert:

1. Check if alert matches any inhibition rule target criteria
2. For matching rules, check if any firing alert matches source criteria
3. Verify ``equal_labels`` match between source and target
4. If inhibited, mark alert but don't send notifications

.. code-block:: python

    def process_new_alert(alert):
        # Create the alert first (for audit trail)
        alert.save()

        # Check inhibition
        if InhibitionService.is_inhibited(alert):
            logger.info(
                f"Alert {alert.id} inhibited by: "
                f"{InhibitionService.get_inhibiting_alerts(alert)}"
            )
            alert.status = 'silenced'  # Or handle as needed
            alert.save()
            return

        # Continue with normal processing
        send_notifications(alert)

Best Practices
--------------

1. **Be specific with equal_labels** - Prevents over-inhibition across environments
2. **Document rule purpose** - Use descriptive names explaining the cascade
3. **Test rules** - Use the test endpoint to verify rule behavior
4. **Monitor inhibition** - Track how often rules fire
5. **Keep rules simple** - Complex chains can hide real issues

Testing Rules
-------------

Test an inhibition rule against an alert:

.. code-block:: python

    # Via API
    POST /api/rules/inhibition/{id}/test/
    {
        "item_type": "alert.api.timeout",
        "severity": "error",
        "labels": {
            "environment": "production",
            "service": "user-api"
        }
    }

    # Response
    {
        "would_be_inhibited": true,
        "inhibiting_alerts": [
            {"id": 123, "title": "Database Connection Failed", "severity": "critical"}
        ]
    }
