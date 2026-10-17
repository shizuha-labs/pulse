=============
Rule Models
=============

Shizuha Hub includes several rule types for automating workflows and managing item behavior.

.. contents:: On This Page
   :local:
   :depth: 2

Rule Overview
-------------

.. mermaid::

    flowchart LR
        subgraph "Rule Types"
            AR[Approval Rules]
            SR[Silence Rules]
            ER[Escalation Rules]
            IR[Inhibition Rules]
        end

        subgraph "Effects"
            AA[Auto-approve/reject actions]
            SN[Suppress notifications]
            ES[Escalate severity]
            SA[Suppress alerts]
        end

        AR --> AA
        SR --> SN
        ER --> ES
        IR --> SA

ApprovalRule
------------

Rules that auto-approve or auto-reject actions based on conditions.

API Reference
^^^^^^^^^^^^^

.. autoclass:: tasks.models.ApprovalRule
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
     - Human-readable rule name
   * - ``description``
     - TextField
     - Detailed description
   * - ``item_types``
     - JSONField
     - List of item type patterns (supports wildcards)
   * - ``sources``
     - JSONField
     - Source whitelist
   * - ``conditions``
     - JSONField
     - JSON Logic conditions
   * - ``auto_approve``
     - BooleanField
     - Auto-approve matching actions
   * - ``auto_reject``
     - BooleanField
     - Auto-reject matching actions
   * - ``priority``
     - IntegerField
     - Evaluation order (higher = first)
   * - ``is_active``
     - BooleanField
     - Whether rule is enabled

Example
^^^^^^^

.. code-block:: python

    from tasks.models import ApprovalRule

    # Auto-approve low-risk hedging actions
    rule = ApprovalRule.objects.create(
        name='Auto-approve small trades',
        item_types=['action.hedging.*'],
        sources=['arbiter-backend'],
        conditions={
            'and': [
                {'<=': [{'var': 'payload.quantity'}, 0.1]},
                {'in': [{'var': 'payload.symbol'}, ['BTCUSDT', 'ETHUSDT']]}
            ]
        },
        auto_approve=True,
        priority=100
    )

SilenceRule
-----------

Suppresses notifications for matching items during specified time windows.

API Reference
^^^^^^^^^^^^^

.. autoclass:: tasks.models.SilenceRule
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
     - Human-readable rule name
   * - ``modes``
     - JSONField
     - Which modes to match (``task``, ``action``, ``alert``)
   * - ``item_types``
     - JSONField
     - Item type patterns
   * - ``sources``
     - JSONField
     - Source whitelist
   * - ``severities``
     - JSONField
     - Severity whitelist (for alerts)
   * - ``label_matchers``
     - JSONField
     - Label key-value matchers (supports wildcards)
   * - ``starts_at``
     - DateTimeField
     - Silence window start
   * - ``ends_at``
     - DateTimeField
     - Silence window end
   * - ``is_recurring``
     - BooleanField
     - Whether silence repeats
   * - ``recurrence_pattern``
     - CharField
     - Cron-like pattern for recurring silences

Example
^^^^^^^

.. code-block:: python

    from tasks.models import SilenceRule
    from django.utils import timezone
    from datetime import timedelta

    # Silence maintenance alerts for 2 hours
    rule = SilenceRule.objects.create(
        name='Maintenance window',
        modes=['alert'],
        sources=['prometheus'],
        label_matchers={
            'environment': 'production',
            'team': 'platform'
        },
        starts_at=timezone.now(),
        ends_at=timezone.now() + timedelta(hours=2),
        reason='Scheduled maintenance'
    )

EscalationRule
--------------

Defines what happens when items aren't handled within specified time thresholds.

API Reference
^^^^^^^^^^^^^

.. autoclass:: tasks.models.EscalationRule
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
     - Human-readable rule name
   * - ``modes``
     - JSONField
     - Which modes to match
   * - ``item_types``
     - JSONField
     - Item type patterns
   * - ``min_severity``
     - CharField
     - Minimum severity to trigger escalation
   * - ``stages``
     - JSONField
     - List of escalation stage configurations
   * - ``is_active``
     - BooleanField
     - Whether rule is enabled

Stage Configuration
^^^^^^^^^^^^^^^^^^^

Each stage in the ``stages`` list can include:

.. code-block:: json

    {
        "delay_minutes": 15,
        "actions": {
            "bump_severity": "critical",
            "add_channels": ["slack", "pagerduty"],
            "set_priority": "urgent",
            "reassign_to": "oncall@company.com",
            "create_action": {
                "item_type": "action.escalation",
                "title": "Escalated: {title}"
            }
        }
    }

Example
^^^^^^^

.. code-block:: python

    from tasks.models import EscalationRule

    rule = EscalationRule.objects.create(
        name='Critical alert escalation',
        modes=['alert'],
        min_severity='error',
        stages=[
            {
                'delay_minutes': 15,
                'actions': {
                    'bump_severity': 'critical',
                    'add_channels': ['slack']
                }
            },
            {
                'delay_minutes': 30,
                'actions': {
                    'add_channels': ['pagerduty'],
                    'set_priority': 'urgent'
                }
            }
        ]
    )

InhibitionRule
--------------

Suppresses target alerts when source alerts are firing (for cascading failures).

API Reference
^^^^^^^^^^^^^

.. autoclass:: tasks.models.InhibitionRule
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
   * - ``source_item_types``
     - JSONField
     - Item types that inhibit
   * - ``source_severities``
     - JSONField
     - Required source severity
   * - ``source_label_matchers``
     - JSONField
     - Source label conditions
   * - ``target_item_types``
     - JSONField
     - Item types to inhibit
   * - ``target_severities``
     - JSONField
     - Target severity filter
   * - ``target_label_matchers``
     - JSONField
     - Target label conditions
   * - ``equal_labels``
     - JSONField
     - Labels that must match between source and target

Example
^^^^^^^

.. code-block:: python

    from tasks.models import InhibitionRule

    # Database down inhibits API timeout alerts
    rule = InhibitionRule.objects.create(
        name='DB down inhibits API',
        source_item_types=['alert.database.*'],
        source_severities=['critical'],
        target_item_types=['alert.api.*'],
        target_label_matchers={'type': 'timeout'},
        equal_labels=['environment', 'cluster']
    )

Rule Evaluation Order
---------------------

Rules are evaluated in the following order:

1. **Inhibition rules** - Check if alert should be suppressed
2. **Silence rules** - Check if notifications should be suppressed
3. **Approval rules** - Check if action should be auto-approved/rejected
4. **Escalation rules** - Check if item should be escalated

Within each category, rules with higher ``priority`` values are evaluated first.
