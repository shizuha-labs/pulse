================
Models Reference
================

Shizuha Hub uses a unified data model approach where all items (tasks, actions, alerts)
are stored in a single ``Item`` model with mode-specific behavior.

Overview
--------

.. mermaid::

    erDiagram
        Organization ||--o{ OrganizationMembership : "memberships"
        Organization ||--o{ Project : "projects"
        Project ||--o{ ProjectMembership : "memberships"
        Project ||--o{ Item : "items"
        Item ||--o{ Item : "parent_item"
        Item ||--o{ Item : "parent_alert"
        Item }o--|| ApprovalRule : "approval_rule"
        Item }o--|| SilenceRule : "silence_rule"
        Item }o--|| EscalationRule : "escalation_rule"

        Organization {
            string name
            string slug
            int owner_id
            string plan
        }

        OrganizationMembership {
            int user_id
            string user_email
            string role
            bool is_pending
        }

        Project {
            string name
            string key
            string access_level
            int next_item_number
        }

        ProjectMembership {
            int user_id
            string role
            bool can_browse
            bool can_create
            bool can_edit
        }

        ApprovalRule {
            string name
            list item_types
            json conditions
            bool auto_approve
        }

        SilenceRule {
            string name
            list modes
            json label_matchers
            datetime starts_at
            datetime ends_at
        }

        EscalationRule {
            string name
            list stages
            int min_severity
        }

        InhibitionRule {
            json source_matchers
            json target_matchers
            list equal_labels
        }

        Subscription {
            string notification_type
            list modes
            json label_matchers
        }

        FingerprintConfig {
            string source
            list label_fields
            list payload_fields
        }

        Item {
            string mode
            string item_type
            string title
            string status
            string priority
            string severity
            json labels
            json payload
        }

Model Categories
----------------

.. grid:: 2
    :gutter: 3

    .. grid-item-card:: Core Model
        :link: item
        :link-type: doc

        The unified ``Item`` model handling tasks, actions, and alerts.

    .. grid-item-card:: Rule Models
        :link: rules
        :link-type: doc

        ``ApprovalRule``, ``SilenceRule``, ``EscalationRule``, ``InhibitionRule``

    .. grid-item-card:: Supporting Models
        :link: supporting
        :link-type: doc

        ``Subscription``, ``FingerprintConfig``

    .. grid-item-card:: Organizations & Projects
        :link: organizations
        :link-type: doc

        ``Organization``, ``OrganizationMembership``, ``Project``, ``ProjectMembership``

.. toctree::
   :maxdepth: 2
   :hidden:

   item
   rules
   supporting
   organizations

Status Choices by Mode
----------------------

.. tabs::

    .. tab:: Task Statuses

        .. list-table::
           :widths: 20 80
           :header-rows: 1

           * - Status
             - Description
           * - ``pending``
             - Task created but not started
           * - ``in_progress``
             - Task is being worked on
           * - ``completed``
             - Task finished successfully
           * - ``cancelled``
             - Task was cancelled

    .. tab:: Action Statuses

        .. list-table::
           :widths: 20 80
           :header-rows: 1

           * - Status
             - Description
           * - ``pending``
             - Awaiting approval
           * - ``approved``
             - Approved, ready to execute
           * - ``rejected``
             - Approval denied
           * - ``executing``
             - Currently executing
           * - ``completed``
             - Executed successfully
           * - ``failed``
             - Execution failed

    .. tab:: Alert Statuses

        .. list-table::
           :widths: 20 80
           :header-rows: 1

           * - Status
             - Description
           * - ``firing``
             - Alert is active
           * - ``acknowledged``
             - Alert acknowledged by operator
           * - ``resolved``
             - Alert condition cleared
           * - ``silenced``
             - Alert suppressed by rule

Priority and Severity
---------------------

**Priority Levels** (all modes):

- ``low`` - Can wait
- ``normal`` - Standard priority (default)
- ``high`` - Needs attention soon
- ``urgent`` - Requires immediate attention

**Severity Levels** (mainly for alerts):

- ``info`` - Informational
- ``warning`` - Warning condition
- ``error`` - Error condition
- ``critical`` - Critical, requires immediate action
