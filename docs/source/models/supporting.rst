=================
Supporting Models
=================

Additional models that support the core functionality of Shizuha Hub.

.. contents:: On This Page
   :local:
   :depth: 2

Subscription
------------

Subscribe to item events for receiving notifications via various channels.

API Reference
^^^^^^^^^^^^^

.. autoclass:: tasks.models.Subscription
   :members:
   :undoc-members:
   :show-inheritance:
   :exclude-members: DoesNotExist, MultipleObjectsReturned

Field Reference
^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``name``
     - CharField
     - Subscription name
   * - ``notification_type``
     - CharField
     - Type: ``webhook``, ``redis``, ``shizuha_notify``
   * - ``webhook_url``
     - URLField
     - Webhook destination (for webhook type)
   * - ``modes``
     - JSONField
     - Modes to subscribe to
   * - ``item_types``
     - JSONField
     - Item type patterns
   * - ``statuses``
     - JSONField
     - Status whitelist
   * - ``severities``
     - JSONField
     - Severity whitelist
   * - ``label_matchers``
     - JSONField
     - Label matching conditions
   * - ``is_active``
     - BooleanField
     - Whether subscription is enabled

Example
^^^^^^^

.. code-block:: python

    from tasks.models import Subscription

    # Webhook subscription for critical alerts
    sub = Subscription.objects.create(
        name='Slack Critical Alerts',
        notification_type='webhook',
        webhook_url='https://hooks.slack.com/services/xxx',
        modes=['alert'],
        statuses=['firing'],
        severities=['critical'],
        label_matchers={
            'environment': 'production'
        }
    )

FingerprintConfig
-----------------

Configures how fingerprints are computed for alerts from different sources.

API Reference
^^^^^^^^^^^^^

.. autoclass:: tasks.models.FingerprintConfig
   :members:
   :undoc-members:
   :show-inheritance:
   :exclude-members: DoesNotExist, MultipleObjectsReturned

Field Reference
^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``source``
     - CharField
     - Source identifier (unique)
   * - ``label_fields``
     - JSONField
     - Labels to include in fingerprint
   * - ``payload_fields``
     - JSONField
     - Payload fields to include in fingerprint
   * - ``is_active``
     - BooleanField
     - Whether config is active

Fingerprint Computation
^^^^^^^^^^^^^^^^^^^^^^^

The fingerprint is computed from:

1. ``item_type`` - Always included
2. ``source`` - Always included
3. Configured ``label_fields`` - Sorted alphabetically
4. Configured ``payload_fields`` - Sorted alphabetically

This creates a deterministic hash for deduplication.

Example
^^^^^^^

.. code-block:: python

    from tasks.models import FingerprintConfig

    # Configure Prometheus alert fingerprinting
    config = FingerprintConfig.objects.create(
        source='prometheus',
        label_fields=['alertname', 'job', 'instance', 'severity'],
        payload_fields=[]  # Don't include payload in fingerprint
    )

    # Configure custom source with payload fields
    config2 = FingerprintConfig.objects.create(
        source='custom-monitor',
        label_fields=['host', 'check_type'],
        payload_fields=['check_id', 'threshold']
    )

Default Behavior
^^^^^^^^^^^^^^^^

If no ``FingerprintConfig`` exists for a source:

- All labels are included in fingerprint
- No payload fields are included
- Order is normalized alphabetically

Backward Compatibility
----------------------

Task Alias
^^^^^^^^^^

For backward compatibility, a ``Task`` alias is provided:

.. code-block:: python

    from tasks.models import Task

    # This is equivalent to:
    from tasks.models import Item as Task

    # Both work identically
    task = Task.objects.create(mode='task', title='My Task')
