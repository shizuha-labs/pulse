===========================
Alert Deduplication Service
===========================

The ``AlertDeduplicationService`` handles alert fingerprinting and deduplication to prevent
duplicate alerts from creating noise.

.. contents:: On This Page
   :local:
   :depth: 2

Overview
--------

When the same alert fires multiple times, deduplication ensures:

1. Only one active alert exists per fingerprint
2. Occurrence count is incremented
3. Timestamps are updated
4. Original alert data is preserved

.. mermaid::

    flowchart TD
        A[Alert Submitted] --> B{Compute Fingerprint}
        B --> C{Active Alert<br/>with Same FP?}
        C -->|Yes| D[Increment Occurrence]
        C -->|No| E[Create New Alert]
        D --> F[Update Timestamps]
        E --> G[Store Fingerprint]
        F --> H[Return Existing Alert]
        G --> I[Return New Alert]

API Reference
-------------

.. autoclass:: tasks.services.deduplication.AlertDeduplicationService
   :members:
   :undoc-members:
   :show-inheritance:

Key Methods
-----------

submit_alert
^^^^^^^^^^^^

The primary entry point for alert submission with automatic deduplication.

.. code-block:: python

    from tasks.services import AlertDeduplicationService

    data = {
        'title': 'High CPU Usage',
        'source': 'prometheus',
        'severity': 'warning',
        'item_type': 'alert.cpu',
        'labels': {
            'host': 'web-01',
            'job': 'node_exporter'
        },
        'payload': {
            'value': 95.5,
            'threshold': 90
        }
    }

    alert, is_new = AlertDeduplicationService.submit_alert(data)

    if is_new:
        print(f"New alert created: {alert.id}")
    else:
        print(f"Deduplicated to alert: {alert.id}, occurrences: {alert.occurrence_count}")

**Returns**: ``Tuple[Item, bool]`` - The alert and whether it's new

compute_fingerprint
^^^^^^^^^^^^^^^^^^^

Computes a deterministic fingerprint for an alert.

.. code-block:: python

    fingerprint = AlertDeduplicationService.compute_fingerprint(
        item_type='alert.cpu',
        source='prometheus',
        labels={'host': 'web-01', 'job': 'node_exporter'}
    )
    # Returns: '8d106e7586e16e9372ffe874ee958612'

handle_recovery
^^^^^^^^^^^^^^^

Processes recovery alerts and resolves matching firing alerts.

.. code-block:: python

    resolved = AlertDeduplicationService.handle_recovery({
        'source': 'prometheus',
        'correlation_id': 'cpu-check-001'
    })

    print(f"Resolved {len(resolved)} alerts")

Fingerprint Configuration
-------------------------

By default, fingerprints are computed from:

- ``item_type``
- ``source``
- All labels (sorted alphabetically)

You can customize fingerprinting per source using ``FingerprintConfig``:

.. code-block:: python

    from tasks.models import FingerprintConfig

    # Only use specific labels for Prometheus
    FingerprintConfig.objects.create(
        source='prometheus',
        label_fields=['alertname', 'job', 'instance'],
        payload_fields=[]
    )

    # Include payload fields for custom source
    FingerprintConfig.objects.create(
        source='custom-monitor',
        label_fields=['host'],
        payload_fields=['check_id']
    )

Deduplication Behavior
----------------------

Active States
^^^^^^^^^^^^^

Alerts with these statuses are considered active for deduplication:

- ``firing``
- ``acknowledged``

Resolved or silenced alerts allow new alerts with the same fingerprint.

Occurrence Tracking
^^^^^^^^^^^^^^^^^^^

When deduplicated, these fields are updated:

- ``occurrence_count`` - Incremented by 1
- ``last_fired_at`` - Updated to current time
- ``payload`` - Merged with new payload (preserves original)
- ``severity`` - Updated if new alert has higher severity

.. code-block:: python

    # First alert
    alert1, _ = AlertDeduplicationService.submit_alert({
        'title': 'High CPU',
        'source': 'prometheus',
        'labels': {'host': 'web-01'}
    })
    # alert1.occurrence_count = 1

    # Same alert again
    alert2, is_new = AlertDeduplicationService.submit_alert({
        'title': 'High CPU',
        'source': 'prometheus',
        'labels': {'host': 'web-01'}
    })
    # is_new = False
    # alert2.id == alert1.id
    # alert2.occurrence_count = 2

Recovery Handling
-----------------

The service supports automatic resolution via recovery signals:

By Correlation ID
^^^^^^^^^^^^^^^^^

.. code-block:: python

    # Original alert with correlation_id
    alert, _ = AlertDeduplicationService.submit_alert({
        'title': 'Database Connection Error',
        'source': 'healthcheck',
        'correlation_id': 'db-check-001',
        'labels': {'db': 'postgres-main'}
    })

    # Later, when issue is resolved
    resolved = AlertDeduplicationService.handle_recovery({
        'source': 'healthcheck',
        'correlation_id': 'db-check-001'
    })
    # alert is now resolved

By Label Matchers
^^^^^^^^^^^^^^^^^

.. code-block:: python

    resolved = AlertDeduplicationService.handle_recovery({
        'source': 'prometheus',
        'resolve_matchers': {
            'host': 'web-01',
            'alertname': 'HighCPU'
        }
    })

Database Constraints
--------------------

A unique constraint ensures only one active alert per fingerprint:

.. code-block:: sql

    CREATE UNIQUE INDEX unique_active_fingerprint
    ON tasks_task (fingerprint)
    WHERE status IN ('firing', 'acknowledged');

This is enforced at the database level for data integrity.

Performance Considerations
--------------------------

1. **Fingerprint Index** - The ``fingerprint`` field is indexed for fast lookups
2. **Label Ordering** - Labels are sorted before hashing for consistency
3. **Batch Processing** - Consider using transactions for bulk alert submission
