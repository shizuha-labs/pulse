========================
Organizations & Projects
========================

This guide covers how to set up and use organizations and projects for
team collaboration in Shizuha Hub.

.. contents:: On This Page
   :local:
   :depth: 2

Overview
--------

Shizuha Hub uses a hierarchical structure for team collaboration:

.. code-block:: text

    Organization (tenant container)
    └── Project (with key like "BE")
        └── Item (with item_key like "BE-123")

**Organizations** are top-level containers that represent teams or companies.
**Projects** are containers within organizations that group related items.
**Items** (tasks, actions, alerts) can be assigned to projects for team visibility.

Creating an Organization
------------------------

Create an organization to start collaborating with your team:

.. code-block:: bash

    curl -X POST "http://localhost:8010/api/organizations/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{
            "name": "My Team",
            "slug": "my-team",
            "description": "Development team workspace"
        }'

Response:

.. code-block:: json

    {
        "id": 1,
        "name": "My Team",
        "slug": "my-team",
        "description": "Development team workspace",
        "owner_id": 1,
        "plan": "free",
        "member_count": 1,
        "project_count": 0
    }

You are automatically added as the organization owner.

Inviting Team Members
---------------------

Invite users to your organization by email:

.. code-block:: bash

    curl -X POST "http://localhost:8010/api/organizations/my-team/members/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{
            "user_email": "colleague@example.com",
            "role": "member"
        }'

**Available Roles:**

.. list-table::
   :widths: 20 80
   :header-rows: 1

   * - Role
     - Description
   * - ``owner``
     - Full access, can delete organization
   * - ``admin``
     - Manage members and projects
   * - ``member``
     - Create and access open/limited projects
   * - ``viewer``
     - View-only access

If the user doesn't exist yet, they'll receive a pending invitation that activates
when they create an account.

Creating Projects
-----------------

Create a project within your organization:

.. code-block:: bash

    curl -X POST "http://localhost:8010/api/projects/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{
            "organization": 1,
            "name": "Backend Development",
            "key": "BE",
            "description": "Backend tasks and features",
            "project_type": "software",
            "access_level": "limited"
        }'

Response:

.. code-block:: json

    {
        "id": 1,
        "organization": 1,
        "name": "Backend Development",
        "key": "BE",
        "access_level": "limited",
        "next_item_number": 1
    }

**Project Key:**
The ``key`` is a short identifier (max 10 characters, uppercase) used for
item keys. For example, items in this project will be ``BE-1``, ``BE-2``, etc.

Project Access Levels
---------------------

Choose the appropriate access level for your project:

.. list-table::
   :widths: 20 40 40
   :header-rows: 1

   * - Level
     - Who Can View
     - Who Can Create/Edit
   * - **Open**
     - All organization members
     - All organization members
   * - **Limited**
     - All organization members
     - Project members only
   * - **Private**
     - Project members only
     - Project members only

**Use Cases:**

- **Open**: Company-wide announcements, general tasks
- **Limited**: Team projects visible to others, editable by team (default)
- **Private**: Confidential projects, HR tasks, sensitive operations

Adding Project Members
----------------------

For limited and private projects, add specific members:

.. code-block:: bash

    curl -X POST "http://localhost:8010/api/projects/1/members/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{
            "user_email": "developer@example.com",
            "role": "member",
            "can_browse": true,
            "can_create": true,
            "can_edit": true,
            "can_delete": false
        }'

**Member Permissions:**

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
     - Update project settings
   * - ``can_manage_members``
     - Add/remove project members

Creating Items in Projects
--------------------------

Create an item (task, action, alert) in a project:

.. code-block:: bash

    curl -X POST "http://localhost:8010/api/items/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{
            "title": "Implement user authentication",
            "mode": "task",
            "project": 1,
            "priority": "high"
        }'

Response:

.. code-block:: json

    {
        "id": 1,
        "item_key": "BE-1",
        "project": 1,
        "project_key": "BE",
        "project_name": "Backend Development",
        "title": "Implement user authentication",
        "status": "pending"
    }

The item receives a unique key (``BE-1``) based on the project key.

Viewing Project Items
---------------------

List all items in a project:

.. code-block:: bash

    curl "http://localhost:8010/api/projects/1/items/" \
        -H "Authorization: Bearer $TOKEN"

Or filter from the items endpoint:

.. code-block:: bash

    curl "http://localhost:8010/api/items/?project=1" \
        -H "Authorization: Bearer $TOKEN"

Kanban Board (Frontend)
-----------------------

The frontend provides a Kanban board view for projects at ``/projects/{id}/board``:

- **Columns**: Pending | In Progress | Completed
- **Drag & Drop**: Move items between columns to change status
- **Quick Edit**: Click items to view/edit details
- **Filters**: Filter by priority, assignee, or item type

Personal Items vs Project Items
-------------------------------

Items can be either personal or project-based:

**Personal Items** (no project):
   - Created without a ``project`` field
   - Only visible to the creator and assignee
   - Work like the original Shizuha Hub behavior

**Project Items**:
   - Created with a ``project`` field
   - Visible based on project membership and access level
   - Have an ``item_key`` like ``BE-123``

Both types can coexist. Use personal items for private tasks and project items
for team collaboration.

Best Practices
--------------

**Organization Structure:**
   - Create one organization per company or team
   - Use descriptive slugs (e.g., ``acme-corp``, ``dev-team``)

**Project Keys:**
   - Keep keys short and memorable (e.g., ``BE``, ``FE``, ``OPS``)
   - Use consistent naming across projects

**Access Levels:**
   - Default to ``limited`` for most projects
   - Use ``private`` sparingly for truly confidential work
   - Use ``open`` for visibility across the organization

**Permissions:**
   - Grant ``can_delete`` only to leads/admins
   - Use ``can_manage_members`` for team leads

Example Workflow
----------------

Here's a complete example of setting up a development team:

.. code-block:: bash

    # 1. Create organization
    curl -X POST "http://localhost:8010/api/organizations/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"name": "Acme Corp", "slug": "acme-corp"}'

    # 2. Invite team members
    curl -X POST "http://localhost:8010/api/organizations/acme-corp/members/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"user_email": "alice@acme.com", "role": "admin"}'

    curl -X POST "http://localhost:8010/api/organizations/acme-corp/members/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"user_email": "bob@acme.com", "role": "member"}'

    # 3. Create projects
    curl -X POST "http://localhost:8010/api/projects/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{
            "organization": 1,
            "name": "Backend",
            "key": "BE",
            "access_level": "limited"
        }'

    curl -X POST "http://localhost:8010/api/projects/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{
            "organization": 1,
            "name": "Frontend",
            "key": "FE",
            "access_level": "limited"
        }'

    # 4. Add members to backend project
    curl -X POST "http://localhost:8010/api/projects/1/members/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"user_email": "bob@acme.com", "role": "member"}'

    # 5. Create tasks
    curl -X POST "http://localhost:8010/api/items/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{
            "title": "Set up database schema",
            "mode": "task",
            "project": 1,
            "priority": "high"
        }'
    # Returns: {"item_key": "BE-1", ...}

    curl -X POST "http://localhost:8010/api/items/" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d '{
            "title": "Design login page",
            "mode": "task",
            "project": 2,
            "priority": "normal"
        }'
    # Returns: {"item_key": "FE-1", ...}

Next Steps
----------

- :doc:`../api/organizations` - Full Organizations API reference
- :doc:`../api/projects` - Full Projects API reference
- :doc:`../models/organizations` - Data model documentation
- :doc:`../services/permissions` - Permission service details
