=================
Organizations API
=================

Organizations are top-level containers that group users and projects together,
similar to workspaces in Jira/Atlassian products.

Overview
--------

Organizations provide:

- Multi-tenant isolation
- Role-based access control (owner, admin, member, viewer)
- Billing and plan management (free, team, business, enterprise)
- Project containers

Endpoints
---------

List Organizations
^^^^^^^^^^^^^^^^^^

.. code-block:: http

    GET /api/organizations/

Returns organizations the authenticated user belongs to.

**Query Parameters:**

- ``is_active`` (bool): Filter by active status

**Response:**

.. code-block:: json

    {
        "count": 2,
        "next": null,
        "previous": null,
        "results": [
            {
                "id": 1,
                "name": "My Workspace",
                "slug": "my-workspace",
                "description": "Personal workspace",
                "logo_url": null,
                "website": null,
                "plan": "free",
                "billing_email": null,
                "owner_id": 1,
                "is_active": true,
                "created_at": "2026-01-28T10:00:00Z",
                "updated_at": "2026-01-28T10:00:00Z",
                "member_count": 3,
                "project_count": 2
            }
        ]
    }

Create Organization
^^^^^^^^^^^^^^^^^^^

.. code-block:: http

    POST /api/organizations/

**Request Body:**

.. code-block:: json

    {
        "name": "My Team",
        "slug": "my-team",
        "description": "Our team workspace"
    }

**Notes:**

- ``slug`` must be unique and URL-safe
- Creator is automatically added as owner
- Defaults to ``free`` plan

Get Organization
^^^^^^^^^^^^^^^^

.. code-block:: http

    GET /api/organizations/{slug}/

Retrieve organization details by slug.

Update Organization
^^^^^^^^^^^^^^^^^^^

.. code-block:: http

    PATCH /api/organizations/{slug}/

**Permissions:** Owner or Admin only

**Request Body:**

.. code-block:: json

    {
        "name": "Updated Name",
        "description": "Updated description"
    }

List Organization Members
^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: http

    GET /api/organizations/{slug}/members/

**Response:**

.. code-block:: json

    {
        "count": 3,
        "members": [
            {
                "id": 1,
                "organization": 1,
                "user_id": 1,
                "user_email": "owner@example.com",
                "role": "owner",
                "is_pending": false,
                "is_active": true,
                "created_at": "2026-01-28T10:00:00Z"
            }
        ]
    }

Invite Member
^^^^^^^^^^^^^

.. code-block:: http

    POST /api/organizations/{slug}/members/

**Permissions:** Owner or Admin only

**Request Body:**

.. code-block:: json

    {
        "user_email": "newmember@example.com",
        "role": "member"
    }

**Notes:**

- Creates a pending invitation with ``is_pending=true`` and ``user_id=null``
- When the invited user logs in and accesses the API, the invitation is automatically accepted
- The system matches by email address and links the user_id to complete the membership
- Valid roles: ``owner``, ``admin``, ``member``, ``viewer``

List Organization Projects
^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: http

    GET /api/organizations/{slug}/projects/

Returns projects in the organization that the user can access.

**Response:**

.. code-block:: json

    {
        "count": 2,
        "projects": [
            {
                "id": 1,
                "name": "Backend",
                "key": "BE",
                "access_level": "open",
                "item_count": 15
            }
        ]
    }

Roles and Permissions
---------------------

.. list-table::
   :widths: 20 20 20 20 20
   :header-rows: 1

   * - Action
     - Owner
     - Admin
     - Member
     - Viewer
   * - View org
     - Yes
     - Yes
     - Yes
     - Yes
   * - Update org
     - Yes
     - Yes
     - No
     - No
   * - Delete org
     - Yes
     - No
     - No
     - No
   * - Invite members
     - Yes
     - Yes
     - No
     - No
   * - Create projects
     - Yes
     - Yes
     - Yes
     - No
   * - View all projects
     - Yes
     - Yes
     - Based on access
     - Based on access

Error Responses
---------------

**403 Forbidden:**

.. code-block:: json

    {
        "detail": "You do not have permission to perform this action."
    }

**404 Not Found:**

.. code-block:: json

    {
        "detail": "Not found."
    }

**400 Bad Request (Duplicate member):**

.. code-block:: json

    {
        "error": "User is already a member"
    }
