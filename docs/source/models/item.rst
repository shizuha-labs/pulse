==========
Item Model
==========

The ``Item`` model is the unified core of Shizuha Hub, handling tasks, actions, and alerts
through a single model with mode-specific behavior.

.. contents:: On This Page
   :local:
   :depth: 2

Overview
--------

The Item model consolidates three types of items:

1. **Tasks** - Personal task management (reminders, to-dos, calendar events)
2. **Actions** - System-generated items requiring approval and execution
3. **Alerts** - System alerts requiring acknowledgment and resolution

This unified approach allows:

- Cross-mode querying and filtering
- Consistent API patterns
- Shared silencing and notification rules
- Unified dashboard views

API Reference
-------------

.. autoclass:: tasks.models.Item
   :members:
   :undoc-members:
   :show-inheritance:
   :exclude-members: DoesNotExist, MultipleObjectsReturned

Field Reference
---------------

Identity Fields
^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``mode``
     - CharField
     - Item mode: ``task``, ``action``, or ``alert``
   * - ``item_type``
     - CharField
     - Hierarchical type (e.g., ``task.reminder``, ``action.hedging.short``)
   * - ``title``
     - CharField
     - Human-readable title
   * - ``description``
     - TextField
     - Detailed description

Source Tracking
^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``source``
     - CharField
     - Origin system (``manual``, ``google_calendar``, ``arbiter-backend``)
   * - ``source_id``
     - CharField
     - External reference ID
   * - ``source_url``
     - URLField
     - Link to source system

Task-Specific Fields
^^^^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``is_recurring``
     - BooleanField
     - Whether this is a recurring task
   * - ``frequency``
     - CharField
     - Recurrence frequency (``daily``, ``weekly``, ``monthly``, etc.)
   * - ``recurrence_pattern``
     - TextField
     - JSON recurrence rules (rrule format)
   * - ``parent_item``
     - ForeignKey
     - Parent recurring task (for instances)
   * - ``estimated_hours``
     - DecimalField
     - Estimated effort
   * - ``actual_hours``
     - DecimalField
     - Actual effort tracked

Action-Specific Fields
^^^^^^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``requires_approval``
     - BooleanField
     - Whether action needs approval
   * - ``auto_approved``
     - BooleanField
     - Whether approved automatically by rule
   * - ``approval_rule``
     - ForeignKey
     - Rule that auto-approved this action
   * - ``callback_url``
     - URLField
     - Webhook URL to call on execution
   * - ``callback_method``
     - CharField
     - HTTP method for callback
   * - ``callback_headers``
     - JSONField
     - Headers for callback request
   * - ``execution_result``
     - JSONField
     - Result from callback execution
   * - ``retry_count``
     - IntegerField
     - Number of execution retries

Alert-Specific Fields
^^^^^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``fingerprint``
     - CharField
     - Computed hash for deduplication
   * - ``occurrence_count``
     - IntegerField
     - Number of times alert fired
   * - ``first_fired_at``
     - DateTimeField
     - First occurrence timestamp
   * - ``last_fired_at``
     - DateTimeField
     - Most recent occurrence
   * - ``escalation_level``
     - IntegerField
     - Current escalation stage
   * - ``correlation_id``
     - CharField
     - ID for grouping related alerts
   * - ``parent_alert``
     - ForeignKey
     - Parent alert (for correlation)

Common Fields
^^^^^^^^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``labels``
     - JSONField
     - Prometheus-style labels (key-value pairs)
   * - ``payload``
     - JSONField
     - Mode-specific data payload
   * - ``tags``
     - CharField
     - Comma-separated tags
   * - ``metadata``
     - JSONField
     - Additional metadata
   * - ``silenced_until``
     - DateTimeField
     - Silence expiration time
   * - ``silenced_by``
     - CharField
     - Who/what silenced this item

Methods
-------

Task Methods
^^^^^^^^^^^^

.. automethod:: tasks.models.Item.mark_completed

Action Methods
^^^^^^^^^^^^^^

.. automethod:: tasks.models.Item.approve
.. automethod:: tasks.models.Item.reject

Alert Methods
^^^^^^^^^^^^^

.. automethod:: tasks.models.Item.acknowledge
.. automethod:: tasks.models.Item.resolve
.. automethod:: tasks.models.Item.silence
.. automethod:: tasks.models.Item.increment_occurrence

Helper Methods
^^^^^^^^^^^^^^

.. automethod:: tasks.models.Item.is_overdue
.. automethod:: tasks.models.Item.is_silenced
.. automethod:: tasks.models.Item.is_active
.. automethod:: tasks.models.Item.days_until_due

Static Methods
^^^^^^^^^^^^^^

.. automethod:: tasks.models.Item.compute_fingerprint

Usage Examples
--------------

Creating Items
^^^^^^^^^^^^^^

.. tabs::

    .. tab:: Task

        .. code-block:: python

            from tasks.models import Item

            task = Item.objects.create(
                mode='task',
                item_type='task.reminder',
                title='Review quarterly report',
                due_date=timezone.now() + timedelta(days=7),
                priority='high',
                tags='work,quarterly'
            )

    .. tab:: Action

        .. code-block:: python

            from tasks.models import Item

            action = Item.objects.create(
                mode='action',
                item_type='action.hedging.short',
                title='Execute hedging trade',
                source='arbiter-backend',
                requires_approval=True,
                payload={
                    'symbol': 'BTCUSDT',
                    'side': 'sell',
                    'quantity': 0.1
                },
                callback_url='https://api.example.com/execute'
            )

    .. tab:: Alert

        .. code-block:: python

            from tasks.models import Item

            alert = Item.objects.create(
                mode='alert',
                item_type='alert.error',
                title='High CPU Usage',
                source='prometheus',
                severity='warning',
                labels={'host': 'web-01', 'service': 'nginx'}
            )

Querying Items
^^^^^^^^^^^^^^

.. code-block:: python

    # Get all active alerts
    firing_alerts = Item.objects.filter(
        mode='alert',
        status__in=['firing', 'acknowledged']
    )

    # Get pending actions requiring approval
    pending_actions = Item.objects.filter(
        mode='action',
        status='pending',
        requires_approval=True
    )

    # Get overdue tasks
    overdue_tasks = Item.objects.filter(
        mode='task',
        status='pending',
        due_date__lt=timezone.now()
    )

Backward Compatibility
----------------------

For backward compatibility, a ``Task`` alias is provided:

.. code-block:: python

    from tasks.models import Task  # Same as Item

    task = Task.objects.create(
        mode='task',
        title='My Task'
    )

Database Indexes
----------------

The Item model includes indexes for common query patterns:

- ``mode`` - Filter by item type
- ``status`` - Filter by status
- ``due_date`` - Date range queries
- ``priority`` - Priority filtering
- ``fingerprint`` - Alert deduplication
- ``source + source_id`` - External reference lookup
- ``assignee_id`` - User assignment queries
