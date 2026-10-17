============
Projects API
============

Projects are containers for related items within an organization, similar to
Jira projects. Each project has a unique key (e.g., "BE") used for item
identifiers (e.g., "BE-123").

Overview
--------

Projects provide:

- Scoped item organization with unique keys
- Configurable access levels (open, limited, private)
- Role-based membership and permissions
- Kanban-style task management

Endpoints
---------

List Projects
^^^^^^^^^^^^^

.. code-block:: http

    GET /api/projects/

Returns projects the authenticated user can access.

**Query Parameters:**

- ``organization`` (int): Filter by organization ID
- ``organization_slug`` (string): Filter by organization slug
- ``include_archived`` (bool): Include archived projects (default: false)

**Response:**

.. code-block:: json

    {
        "count": 2,
        "next": null,
        "previous": null,
        "results": [
            {
                "id": 1,
                "organization": 1,
                "organization_name": "My Workspace",
                "organization_slug": "my-workspace",
                "name": "Backend Development",
                "key": "BE",
                "description": "Backend tasks and features",
                "project_type": "software",
                "access_level": "limited",
                "lead_id": 1,
                "next_item_number": 42,
                "color": "#0052CC",
                "is_archived": false,
                "member_count": 3,
                "item_count": 41
            }
        ]
    }

Create Project
^^^^^^^^^^^^^^

.. code-block:: http

    POST /api/projects/

**Request Body:**

.. code-block:: json

    {
        "organization": 1,
        "name": "Backend Development",
        "key": "BE",
        "description": "Backend tasks and features",
        "project_type": "software",
        "access_level": "limited"
    }

**Notes:**

- ``key`` must be unique within the organization (max 10 chars, uppercase)
- Creator is automatically added as project admin
- Valid project types: ``software``, ``business``, ``service_management``, ``operations``
- Valid access levels: ``open``, ``limited``, ``private``

Get Project
^^^^^^^^^^^

.. code-block:: http

    GET /api/projects/{id}/

Retrieve project details.

Update Project
^^^^^^^^^^^^^^

.. code-block:: http

    PATCH /api/projects/{id}/

**Permissions:** Project admin or has ``can_manage_project`` permission

**Request Body:**

.. code-block:: json

    {
        "name": "Updated Name",
        "description": "Updated description",
        "access_level": "open"
    }

List Project Members
^^^^^^^^^^^^^^^^^^^^

.. code-block:: http

    GET /api/projects/{id}/members/

**Response:**

.. code-block:: json

    {
        "count": 3,
        "members": [
            {
                "id": 1,
                "project": 1,
                "user_id": 1,
                "user_email": "dev@example.com",
                "role": "admin",
                "can_browse": true,
                "can_create": true,
                "can_edit": true,
                "can_delete": true,
                "can_manage_project": true,
                "can_manage_members": true,
                "is_active": true
            }
        ]
    }

Add Project Member
^^^^^^^^^^^^^^^^^^

.. code-block:: http

    POST /api/projects/{id}/members/

**Permissions:** Has ``can_manage_members`` permission

**Request Body:**

.. code-block:: json

    {
        "user_email": "newmember@example.com",
        "role": "member",
        "can_browse": true,
        "can_create": true,
        "can_edit": true,
        "can_delete": false
    }

**Notes:**

- User must be a member of the organization first
- Can add by ``user_email`` - if the user hasn't logged in yet, the membership is linked by email
- When the invited user logs in, their ``user_id`` is automatically linked to the membership
- Valid roles: ``admin``, ``member``, ``viewer``

List Project Items
^^^^^^^^^^^^^^^^^^

.. code-block:: http

    GET /api/projects/{id}/items/

Returns items in the project.

**Query Parameters:**

- ``mode`` (string): Filter by mode (task, action, alert)
- ``status`` (string): Filter by status
- ``priority`` (string): Filter by priority

**Response:**

.. code-block:: json

    {
        "count": 15,
        "items": [
            {
                "id": 1,
                "item_key": "BE-1",
                "title": "Implement user authentication",
                "status": "in_progress",
                "priority": "high"
            }
        ]
    }

Archive Project
^^^^^^^^^^^^^^^

.. code-block:: http

    POST /api/projects/{id}/archive/

**Permissions:** Has ``can_manage_project`` permission

Archives the project. Archived projects are hidden by default but can be restored.

Unarchive Project
^^^^^^^^^^^^^^^^^

.. code-block:: http

    POST /api/projects/{id}/unarchive/

**Permissions:** Has ``can_manage_project`` permission

Restores an archived project.

Access Levels
-------------

.. list-table::
   :widths: 20 80
   :header-rows: 1

   * - Level
     - Description
   * - **Open**
     - All organization members can view and create items
   * - **Limited**
     - Only project members can create/edit, org members can view
   * - **Private**
     - Only project members can view and interact

Member Permissions
------------------

Each project member has granular permissions:

.. list-table::
   :widths: 25 75
   :header-rows: 1

   * - Permission
     - Description
   * - ``can_browse``
     - View project and its items
   * - ``can_create``
     - Create new items in the project
   * - ``can_edit``
     - Edit existing items
   * - ``can_delete``
     - Delete items
   * - ``can_manage_project``
     - Update project settings, archive/unarchive
   * - ``can_manage_members``
     - Add/remove project members

Default Permissions by Role
^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. list-table::
   :widths: 20 15 15 15 15 15 15
   :header-rows: 1

   * - Role
     - Browse
     - Create
     - Edit
     - Delete
     - Manage Project
     - Manage Members
   * - Admin
     - Yes
     - Yes
     - Yes
     - Yes
     - Yes
     - Yes
   * - Member
     - Yes
     - Yes
     - Yes
     - No
     - No
     - No
   * - Viewer
     - Yes
     - No
     - No
     - No
     - No
     - No

Item Keys
---------

Items created in a project receive a unique key combining the project key
and a sequential number:

.. code-block:: text

    {PROJECT_KEY}-{NUMBER}

Example: ``BE-1``, ``BE-2``, ``BE-123``

The number is atomically incremented to ensure uniqueness even under
concurrent item creation.

Creating Items in Projects
--------------------------

To create an item in a project, include the ``project`` field:

.. code-block:: http

    POST /api/items/

**Request Body:**

.. code-block:: json

    {
        "title": "Implement user authentication",
        "mode": "task",
        "project": 1,
        "priority": "high"
    }

**Response:**

.. code-block:: json

    {
        "id": 1,
        "item_key": "BE-1",
        "project": 1,
        "project_key": "BE",
        "project_name": "Backend Development",
        "organization_slug": "my-workspace",
        "title": "Implement user authentication",
        "status": "pending"
    }

Filtering Items by Project
--------------------------

.. code-block:: http

    GET /api/items/?project=1

Query parameters:

- ``project`` (int): Filter by project ID
- ``organization`` (int): Filter by organization ID
- ``include_personal`` (bool): Include personal items without a project (default: true)

Error Responses
---------------

**403 Forbidden (No permission to create):**

.. code-block:: json

    {
        "project": "You do not have permission to create items in this project"
    }

**400 Bad Request (Duplicate key):**

.. code-block:: json

    {
        "key": ["Project with this key already exists in the organization"]
    }

**400 Bad Request (Not org member):**

.. code-block:: json

    {
        "error": "User is not a member of the organization"
    }
