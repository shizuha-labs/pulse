=================
Silencing Service
=================

The ``SilencingService`` evaluates silence rules and manages notification suppression
during maintenance windows or known issues.

.. contents:: On This Page
   :local:
   :depth: 2

Overview
--------

Silencing allows you to:

1. Suppress notifications for matching items
2. Schedule maintenance windows
3. Handle known issues without noise
4. Work across all item modes

.. important::

    Silenced items are still **created and tracked**. Only notifications are suppressed.
    This preserves the audit trail and allows post-maintenance review.

.. mermaid::

    flowchart TD
        A[Item Created] --> B{Check Silence Rules}
        B --> C{Any Rule Matches?}
        C -->|Yes| D[Mark Item Silenced]
        C -->|No| E[Send Notifications]
        D --> F[Skip Notifications]
        D --> G[Log Silence Match]
        F --> H[Item Still Tracked]

API Reference
-------------

.. autoclass:: tasks.services.silencing.SilencingService
   :members:
   :undoc-members:
   :show-inheritance:

Key Methods
-----------

is_silenced
^^^^^^^^^^^

Checks if an item is currently silenced by any rule.

.. code-block:: python

    from tasks.services import SilencingService
    from tasks.models import Item

    alert = Item.objects.get(id=123)

    if SilencingService.is_silenced(alert):
        print("Item is silenced, skip notifications")
    else:
        send_notification(alert)

find_matching_rule
^^^^^^^^^^^^^^^^^^

Returns the first matching silence rule (if any).

.. code-block:: python

    rule = SilencingService.find_matching_rule(alert)

    if rule:
        print(f"Matched rule: {rule.name}")
        print(f"Reason: {rule.reason}")

silence_item
^^^^^^^^^^^^

Manually silence an individual item.

.. code-block:: python

    from django.utils import timezone
    from datetime import timedelta

    # Silence for 1 hour
    SilencingService.silence_item(
        item=alert,
        duration_minutes=60,
        silenced_by='user:admin',
        reason='Investigating issue'
    )

    # Or specify end time
    SilencingService.silence_item(
        item=alert,
        until=timezone.now() + timedelta(hours=2),
        silenced_by='user:admin',
        reason='Maintenance window'
    )

create_silence_rule
^^^^^^^^^^^^^^^^^^^

Create a silence rule programmatically.

.. code-block:: python

    from datetime import timedelta
    from django.utils import timezone

    rule = SilencingService.create_silence_rule(
        name='Scheduled Maintenance',
        modes=['alert'],
        label_matchers={'environment': 'staging'},
        starts_at=timezone.now(),
        ends_at=timezone.now() + timedelta(hours=4),
        reason='Deploying new version'
    )

Matching Rules
--------------

Label Matchers
^^^^^^^^^^^^^^

Label matchers support exact and wildcard matching:

.. code-block:: python

    from tasks.models import SilenceRule

    # Exact match
    rule = SilenceRule.objects.create(
        name='Silence web-01',
        label_matchers={'host': 'web-01'}
    )

    # Wildcard match
    rule = SilenceRule.objects.create(
        name='Silence all web servers',
        label_matchers={'host': 'web-*'}
    )

    # Multiple labels (AND logic)
    rule = SilenceRule.objects.create(
        name='Silence staging web',
        label_matchers={
            'environment': 'staging',
            'type': 'web-*'
        }
    )

Item Type Matching
^^^^^^^^^^^^^^^^^^

Item types support wildcard patterns:

.. code-block:: python

    rule = SilenceRule.objects.create(
        name='Silence all database alerts',
        item_types=['alert.database.*']
    )

    rule = SilenceRule.objects.create(
        name='Silence specific alert types',
        item_types=['alert.cpu', 'alert.memory', 'alert.disk']
    )

Time Windows
------------

Basic Time Window
^^^^^^^^^^^^^^^^^

.. code-block:: python

    rule = SilenceRule.objects.create(
        name='Maintenance Window',
        starts_at=timezone.now(),
        ends_at=timezone.now() + timedelta(hours=2),
        modes=['alert']
    )

Recurring Silence
^^^^^^^^^^^^^^^^^

For recurring maintenance windows:

.. code-block:: python

    rule = SilenceRule.objects.create(
        name='Weekly Maintenance',
        is_recurring=True,
        recurrence_pattern='0 2 * * 0',  # Every Sunday at 2 AM
        modes=['alert']
    )

Silence Management
------------------

Getting Active Silences
^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    active_rules = SilencingService.get_active_silences()

    for rule in active_rules:
        print(f"{rule.name}: {rule.starts_at} - {rule.ends_at}")

Cleanup Expired Silences
^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    # Deactivate expired rules
    deactivated = SilencingService.cleanup_expired_silences()
    print(f"Deactivated {deactivated} expired rules")

Item-Level Silencing
--------------------

Items can be silenced individually without rules:

.. code-block:: python

    # Via API endpoint
    POST /api/items/{id}/silence/
    {
        "duration_minutes": 60,
        "reason": "Known issue, fix in progress"
    }

    # Or directly
    from django.utils import timezone
    from datetime import timedelta

    alert.silence(
        until=timezone.now() + timedelta(hours=1),
        silenced_by='user:oncall',
        reason='Investigating'
    )

Silenced items have these fields populated:

- ``silenced_until`` - When silence expires
- ``silenced_by`` - Who/what silenced it
- ``silence_reason`` - Why it was silenced
- ``silence_rule`` - Rule that matched (if rule-based)

Integration with Notifications
------------------------------

The silencing service is typically checked before sending notifications:

.. code-block:: python

    def process_new_alert(alert):
        # Check silencing
        if SilencingService.is_silenced(alert):
            logger.info(f"Alert {alert.id} silenced, skipping notifications")
            return

        # Check inhibition
        if InhibitionService.is_inhibited(alert):
            return

        # Send notifications
        for subscription in Subscription.objects.filter(is_active=True):
            if subscription.matches(alert):
                send_notification(subscription, alert)

Best Practices
--------------

1. **Use descriptive names** - Make it clear why silence exists
2. **Set expiration** - Always set ``ends_at`` to avoid forgotten silences
3. **Include reason** - Document why the silence was created
4. **Be specific** - Use specific label matchers to avoid over-silencing
5. **Review regularly** - Periodically audit active silences
