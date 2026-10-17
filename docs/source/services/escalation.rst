==================
Escalation Service
==================

The ``EscalationService`` handles time-based escalation when items aren't resolved within
specified thresholds.

.. contents:: On This Page
   :local:
   :depth: 2

Overview
--------

Escalation ensures critical items receive appropriate attention by:

1. Bumping severity levels over time
2. Adding notification channels
3. Reassigning to different teams
4. Creating follow-up actions

.. mermaid::

    flowchart TD
        A[Alert Created] --> B{Matches<br/>Escalation Rule?}
        B -->|No| C[No Escalation]
        B -->|Yes| D[Schedule Stage 1]
        D --> E{Delay Elapsed?}
        E -->|No| F[Wait]
        F --> E
        E -->|Yes| G[Apply Stage Actions]
        G --> H{More Stages?}
        H -->|Yes| I[Schedule Next Stage]
        I --> E
        H -->|No| J[Max Escalation Reached]

API Reference
-------------

.. autoclass:: tasks.services.escalation.EscalationService
   :members:
   :undoc-members:
   :show-inheritance:

Key Methods
-----------

find_matching_rule
^^^^^^^^^^^^^^^^^^

Finds the first escalation rule that matches an item.

.. code-block:: python

    from tasks.services import EscalationService
    from tasks.models import Item

    alert = Item.objects.get(id=123)
    rule = EscalationService.find_matching_rule(alert)

    if rule:
        print(f"Matched rule: {rule.name}")

should_escalate
^^^^^^^^^^^^^^^

Checks if an item should be escalated based on time thresholds.

.. code-block:: python

    if EscalationService.should_escalate(alert, rule):
        EscalationService.escalate_item(alert, rule)

escalate_item
^^^^^^^^^^^^^

Performs the escalation actions for the current stage.

.. code-block:: python

    # Escalate to next level
    EscalationService.escalate_item(alert, rule)

    # Alert is now updated with:
    # - Higher severity (if configured)
    # - Additional notification channels
    # - Updated priority
    # - Incremented escalation_level

process_pending_escalations
^^^^^^^^^^^^^^^^^^^^^^^^^^^

Processes all items that need escalation (called by scheduler).

.. code-block:: python

    # Typically called by Celery beat
    escalated_count = EscalationService.process_pending_escalations()
    print(f"Escalated {escalated_count} items")

Escalation Stages
-----------------

Each stage defines a delay and actions to take:

Stage Configuration
^^^^^^^^^^^^^^^^^^^

.. code-block:: json

    {
        "delay_minutes": 15,
        "actions": {
            "bump_severity": "critical",
            "add_channels": ["slack", "pagerduty"],
            "set_priority": "urgent",
            "reassign_to": "oncall@company.com",
            "create_action": {
                "item_type": "action.escalation.follow_up",
                "title": "ESCALATED: {title}",
                "requires_approval": false
            }
        }
    }

Available Actions
^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 30 70
   :header-rows: 1

   * - Action
     - Description
   * - ``bump_severity``
     - Increase severity to specified level
   * - ``add_channels``
     - Add notification channels
   * - ``set_priority``
     - Set priority level
   * - ``reassign_to``
     - Reassign to user/team
   * - ``create_action``
     - Create follow-up action item

Example Configuration
---------------------

Three-Stage Escalation
^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    from tasks.models import EscalationRule

    rule = EscalationRule.objects.create(
        name='Critical Production Alerts',
        modes=['alert'],
        item_types=['alert.*'],
        min_severity='error',
        label_matchers={'environment': 'production'},
        stages=[
            # Stage 1: After 15 minutes
            {
                'delay_minutes': 15,
                'actions': {
                    'bump_severity': 'critical',
                    'add_channels': ['slack']
                }
            },
            # Stage 2: After 30 minutes total
            {
                'delay_minutes': 30,
                'actions': {
                    'add_channels': ['pagerduty'],
                    'set_priority': 'urgent'
                }
            },
            # Stage 3: After 60 minutes total
            {
                'delay_minutes': 60,
                'actions': {
                    'reassign_to': 'engineering-lead@company.com',
                    'create_action': {
                        'item_type': 'action.escalation.executive_review',
                        'title': 'CRITICAL: {title} - Requires Executive Review'
                    }
                }
            }
        ]
    )

Business Hours Only
^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    rule = EscalationRule.objects.create(
        name='Business Hours Escalation',
        modes=['alert'],
        conditions={
            'and': [
                {'>=': [{'var': 'hour'}, 9]},
                {'<=': [{'var': 'hour'}, 17]},
                {'in': [{'var': 'weekday'}, [1, 2, 3, 4, 5]]}
            ]
        },
        stages=[
            {
                'delay_minutes': 30,
                'actions': {'add_channels': ['slack']}
            }
        ]
    )

Scheduling
----------

Escalation checking can be scheduled via Celery Beat:

.. code-block:: python

    # In celery beat schedule
    CELERY_BEAT_SCHEDULE = {
        'process-escalations': {
            'task': 'tasks.tasks.process_escalations',
            'schedule': crontab(minute='*/5'),  # Every 5 minutes
        },
    }

Or triggered directly:

.. code-block:: python

    from tasks.services import EscalationService

    # Process all pending escalations
    EscalationService.process_pending_escalations()

Escalation Tracking
-------------------

Items track their escalation state:

.. list-table::
   :widths: 30 70
   :header-rows: 1

   * - Field
     - Description
   * - ``escalation_level``
     - Current stage (0 = not escalated)
   * - ``last_escalated_at``
     - Timestamp of last escalation
   * - ``escalation_rule``
     - Rule that escalated this item

Preventing Re-escalation
------------------------

Items are not escalated if:

1. Status is resolved/completed
2. Item is acknowledged (resets escalation timer)
3. Item is silenced
4. No matching escalation rule

.. code-block:: python

    # Acknowledging resets escalation timer
    alert.acknowledge(acknowledged_by='user:admin')
    # alert.last_escalated_at is reset
    # Escalation clock restarts from Stage 1
