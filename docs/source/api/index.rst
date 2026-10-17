=============
API Reference
=============

Shizuha Hub provides a REST API built with Django REST Framework for managing
tasks, actions, and alerts.

Base URL
--------

All API endpoints are prefixed with:

.. code-block:: text

    /shizuha-pulse/api/

For example: ``http://localhost:8010/shizuha-pulse/api/items/``

Authentication
--------------

.. warning::

    Authentication is currently disabled for development. Implement proper
    authentication before production deployment.

Content Type
------------

All requests should use:

.. code-block:: text

    Content-Type: application/json

API Sections
------------

.. grid:: 2
    :gutter: 3

    .. grid-item-card:: Organizations API
        :link: organizations
        :link-type: doc

        Multi-tenant organization management and membership.

    .. grid-item-card:: Projects API
        :link: projects
        :link-type: doc

        Project management with Jira-style item keys.

    .. grid-item-card:: Items API
        :link: items
        :link-type: doc

        Unified CRUD operations for tasks, actions, and alerts.

    .. grid-item-card:: Alerts API
        :link: alerts
        :link-type: doc

        Alert submission, bulk operations, and deduplication.

    .. grid-item-card:: Actions API
        :link: actions
        :link-type: doc

        Action submission and approval workflows.

    .. grid-item-card:: Rules API
        :link: rules
        :link-type: doc

        Manage approval, silence, escalation, and inhibition rules.

    .. grid-item-card:: Comments API
        :link: comments
        :link-type: doc

        Discussion and collaboration on items.

    .. grid-item-card:: OpenAPI Spec
        :link: openapi
        :link-type: doc

        Auto-generated OpenAPI/Swagger documentation.

.. toctree::
   :maxdepth: 2
   :hidden:

   organizations
   projects
   comments
   items
   alerts
   actions
   rules
   openapi

Quick Reference
---------------

Organization Endpoints
^^^^^^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 10 30 60
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - GET
     - ``/organizations/``
     - List user's organizations
   * - POST
     - ``/organizations/``
     - Create organization
   * - GET
     - ``/organizations/{slug}/``
     - Get organization details
   * - PATCH
     - ``/organizations/{slug}/``
     - Update organization
   * - GET
     - ``/organizations/{slug}/members/``
     - List members
   * - POST
     - ``/organizations/{slug}/members/``
     - Invite member
   * - GET
     - ``/organizations/{slug}/projects/``
     - List projects

Project Endpoints
^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 10 30 60
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - GET
     - ``/projects/``
     - List accessible projects
   * - POST
     - ``/projects/``
     - Create project
   * - GET
     - ``/projects/{id}/``
     - Get project details
   * - PATCH
     - ``/projects/{id}/``
     - Update project
   * - GET
     - ``/projects/{id}/members/``
     - List project members
   * - POST
     - ``/projects/{id}/members/``
     - Add project member
   * - GET
     - ``/projects/{id}/items/``
     - List project items
   * - POST
     - ``/projects/{id}/archive/``
     - Archive project
   * - POST
     - ``/projects/{id}/unarchive/``
     - Unarchive project

Comments Endpoints
^^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 10 30 60
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - GET
     - ``/comments/?item={id}``
     - List comments for an item
   * - POST
     - ``/comments/``
     - Create comment
   * - PATCH
     - ``/comments/{id}/``
     - Update comment (own only)
   * - DELETE
     - ``/comments/{id}/``
     - Delete comment (own only)

Items Endpoints
^^^^^^^^^^^^^^^

.. list-table::
   :widths: 10 30 60
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - GET
     - ``/items/``
     - List all items
   * - POST
     - ``/items/``
     - Create new item
   * - GET
     - ``/items/{id}/``
     - Get item details
   * - PUT
     - ``/items/{id}/``
     - Update item
   * - DELETE
     - ``/items/{id}/``
     - Delete item
   * - GET
     - ``/items/tasks/``
     - List tasks only
   * - GET
     - ``/items/actions/``
     - List actions only
   * - GET
     - ``/items/alerts/``
     - List active alerts
   * - GET
     - ``/items/pending/``
     - List pending items
   * - GET
     - ``/items/statistics/``
     - Get item statistics

Item Actions
^^^^^^^^^^^^

.. list-table::
   :widths: 10 30 60
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - POST
     - ``/items/{id}/complete/``
     - Mark task as completed
   * - POST
     - ``/items/{id}/approve/``
     - Approve action
   * - POST
     - ``/items/{id}/reject/``
     - Reject action
   * - POST
     - ``/items/{id}/acknowledge/``
     - Acknowledge alert
   * - POST
     - ``/items/{id}/resolve/``
     - Resolve alert
   * - POST
     - ``/items/{id}/silence/``
     - Silence item

Alert Endpoints
^^^^^^^^^^^^^^^

.. list-table::
   :widths: 10 30 60
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - POST
     - ``/alerts/``
     - Submit alert (with dedup)
   * - POST
     - ``/alerts/bulk-acknowledge/``
     - Bulk acknowledge
   * - POST
     - ``/alerts/bulk-resolve/``
     - Bulk resolve

Action Endpoints
^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 10 30 60
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - POST
     - ``/actions/submit/``
     - Submit new action

Rule Endpoints
^^^^^^^^^^^^^^

.. list-table::
   :widths: 10 30 60
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - CRUD
     - ``/rules/approval/``
     - Approval rules
   * - CRUD
     - ``/rules/silence/``
     - Silence rules
   * - CRUD
     - ``/rules/escalation/``
     - Escalation rules
   * - CRUD
     - ``/rules/inhibition/``
     - Inhibition rules

Common Response Formats
-----------------------

Success Response
^^^^^^^^^^^^^^^^

.. code-block:: json

    {
        "id": 123,
        "mode": "task",
        "title": "My Task",
        "status": "pending",
        ...
    }

List Response
^^^^^^^^^^^^^

.. code-block:: json

    {
        "count": 42,
        "next": "http://localhost:8010/shizuha-pulse/api/items/?page=2",
        "previous": null,
        "results": [
            {...},
            {...}
        ]
    }

Error Response
^^^^^^^^^^^^^^

.. code-block:: json

    {
        "error": "Item not found",
        "detail": "No item with ID 999 exists"
    }

Validation Error
^^^^^^^^^^^^^^^^

.. code-block:: json

    {
        "title": ["This field is required."],
        "due_date": ["Invalid date format."]
    }
