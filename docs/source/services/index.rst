==================
Services Reference
==================

Shizuha Hub uses service classes to encapsulate business logic for key operations.

Overview
--------

.. mermaid::

    flowchart TB
        subgraph "Alert Processing"
            SUBMIT[Submit Alert]
            DEDUP[AlertDeduplicationService]
            INH[InhibitionService]
            SIL[SilencingService]
            ESC[EscalationService]
        end

        SUBMIT --> DEDUP
        DEDUP --> |new alert| INH
        INH --> |not inhibited| SIL
        SIL --> |not silenced| ESC
        ESC --> |if needed| ESCALATE[Escalate]

Service Categories
------------------

.. grid:: 2
    :gutter: 3

    .. grid-item-card:: Alert Deduplication
        :link: deduplication
        :link-type: doc

        Fingerprint-based alert deduplication and occurrence tracking.

    .. grid-item-card:: Escalation
        :link: escalation
        :link-type: doc

        Time-based escalation with severity bumping and notifications.

    .. grid-item-card:: Silencing
        :link: silencing
        :link-type: doc

        Notification suppression based on configurable rules.

    .. grid-item-card:: Inhibition
        :link: inhibition
        :link-type: doc

        Alert suppression during cascading failures.

    .. grid-item-card:: Permissions
        :link: permissions
        :link-type: doc

        Organization and project permission checking for multi-tenant access control.

.. toctree::
   :maxdepth: 2
   :hidden:

   deduplication
   escalation
   silencing
   inhibition
   permissions

Service Integration
-------------------

Services are designed to be used together in the alert processing pipeline:

.. code-block:: python

    from tasks.services import (
        AlertDeduplicationService,
        InhibitionService,
        SilencingService,
        EscalationService
    )

    # 1. Submit and deduplicate alert
    alert, is_new = AlertDeduplicationService.submit_alert(alert_data)

    if is_new:
        # 2. Check inhibition
        if InhibitionService.is_inhibited(alert):
            alert.status = 'silenced'
            alert.save()
            return

        # 3. Check silencing rules
        if SilencingService.is_silenced(alert):
            # Still create alert, just suppress notifications
            alert.silenced_by = 'rule:matching_rule'
            alert.save()
        else:
            # 4. Send notifications
            notify_subscribers(alert)

        # 5. Schedule escalation if needed
        EscalationService.schedule_escalation(alert)

Common Patterns
---------------

All services follow these patterns:

**Class Methods**
    Services use ``@classmethod`` for all public methods, no instantiation required.

**Rule Matching**
    Pattern matching for item types supports wildcards (e.g., ``alert.db.*``).

**Label Matching**
    Label matchers support:
    - Exact match: ``{"host": "web-01"}``
    - Wildcard: ``{"host": "web-*"}``
    - Regex (future): ``{"host": "~web-\\d+"}``

**Logging**
    All services log operations via Python's ``logging`` module.

Error Handling
--------------

Services are designed to be resilient:

- Missing configurations use sensible defaults
- Invalid rule configurations are skipped with warnings
- Database errors are propagated for transaction rollback
