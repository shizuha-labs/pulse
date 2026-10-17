==========================
Organizations and Projects
==========================

Shizuha Hub uses a hierarchical structure for multi-tenant organization:

.. code-block:: text

    Organization (tenant container)
    └── Project (with key like "PROJ")
        └── Item (with item_key like "PROJ-123")

Organization Model
------------------

Organizations are top-level containers that group users and their work.

Fields
^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``name``
     - CharField(255)
     - Display name of the organization
   * - ``slug``
     - SlugField(100)
     - URL-safe unique identifier
   * - ``description``
     - TextField
     - Organization description
   * - ``logo_url``
     - URLField
     - URL to organization logo
   * - ``website``
     - URLField
     - Organization website
   * - ``plan``
     - CharField(50)
     - Subscription plan: free, team, business, enterprise
   * - ``billing_email``
     - EmailField
     - Billing contact email
   * - ``owner_id``
     - IntegerField
     - Primary owner's user ID
   * - ``settings``
     - JSONField
     - Organization-wide settings
   * - ``is_active``
     - BooleanField
     - Whether organization is active

Example
^^^^^^^

.. code-block:: python

    org = Organization.objects.create(
        name="Acme Corp",
        slug="acme-corp",
        owner_id=user.id,
        plan="team"
    )

OrganizationMembership Model
----------------------------

Links users to organizations with specific roles.

Fields
^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``organization``
     - ForeignKey
     - Related organization
   * - ``user_id``
     - IntegerField
     - User ID (NULL for pending invitations)
   * - ``user_email``
     - EmailField
     - User's email address
   * - ``role``
     - CharField(20)
     - Role: owner, admin, member, viewer
   * - ``is_pending``
     - BooleanField
     - Whether this is a pending invitation
   * - ``invited_by_id``
     - IntegerField
     - ID of user who sent invitation
   * - ``invited_at``
     - DateTimeField
     - When invitation was sent
   * - ``accepted_at``
     - DateTimeField
     - When invitation was accepted

Methods
^^^^^^^

.. method:: accept_invitation(user)

    Accept a pending invitation and link it to a user.

    :param user: Django User object
    :raises ValueError: If membership is not pending

Roles
^^^^^

.. list-table::
   :widths: 20 80
   :header-rows: 1

   * - Role
     - Capabilities
   * - **Owner**
     - Full control, can delete organization
   * - **Admin**
     - Manage members and settings
   * - **Member**
     - Create projects and items
   * - **Viewer**
     - Read-only access

Project Model
-------------

Projects contain related items within an organization.

Fields
^^^^^^

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``organization``
     - ForeignKey
     - Parent organization
   * - ``name``
     - CharField(255)
     - Project name
   * - ``key``
     - CharField(10)
     - Project key for item numbering (e.g., "PROJ")
   * - ``description``
     - TextField
     - Project description
   * - ``project_type``
     - CharField(30)
     - Type: software, business, service_management, operations
   * - ``access_level``
     - CharField(20)
     - Access: open, limited, private
   * - ``lead_id``
     - IntegerField
     - Project lead's user ID
   * - ``default_assignee_id``
     - IntegerField
     - Default assignee for new items
   * - ``next_item_number``
     - PositiveIntegerField
     - Counter for item key generation
   * - ``color``
     - CharField(7)
     - Project color (hex)
   * - ``icon``
     - CharField(50)
     - Project icon name
   * - ``is_archived``
     - BooleanField
     - Whether project is archived

Methods
^^^^^^^

.. method:: get_next_item_key()

    Atomically generate the next item key for this project.

    :returns: String like "PROJ-123"

    Uses database-level locking to ensure unique keys under concurrent access.

    .. code-block:: python

        key = project.get_next_item_key()  # Returns "BE-1"
        key = project.get_next_item_key()  # Returns "BE-2"

Access Levels
^^^^^^^^^^^^^

.. list-table::
   :widths: 20 80
   :header-rows: 1

   * - Level
     - Description
   * - **Open**
     - All organization members can view and create items
   * - **Limited**
     - Organization members can view; only project members can create/edit
   * - **Private**
     - Only project members can access

ProjectMembership Model
-----------------------

Links users to projects with granular permissions.

Fields
^^^^^^

.. list-table::
   :widths: 25 20 55
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``project``
     - ForeignKey
     - Related project
   * - ``user_id``
     - IntegerField
     - User ID
   * - ``user_email``
     - EmailField
     - User's email
   * - ``role``
     - CharField(20)
     - Role: admin, member, viewer
   * - ``can_browse``
     - BooleanField
     - Can view project items
   * - ``can_create``
     - BooleanField
     - Can create items
   * - ``can_edit``
     - BooleanField
     - Can edit items
   * - ``can_delete``
     - BooleanField
     - Can delete items
   * - ``can_manage_project``
     - BooleanField
     - Can update project settings
   * - ``can_manage_members``
     - BooleanField
     - Can add/remove members

Item Integration
----------------

The Item model includes fields for project association:

.. list-table::
   :widths: 20 20 60
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``project``
     - ForeignKey
     - Related project (NULL for personal items)
   * - ``item_key``
     - CharField(50)
     - Unique key like "PROJ-123"

Personal Items
^^^^^^^^^^^^^^

Items without a ``project`` are considered personal items:

- Only visible to their creator/assignee
- No item_key is generated
- Backward compatible with existing items

Project Items
^^^^^^^^^^^^^

Items with a ``project`` are visible based on project access rules:

- Automatically assigned an ``item_key``
- Visibility determined by project membership and access level
- Can be viewed/edited by authorized project members

Database Schema
---------------

.. code-block:: sql

    -- Organizations
    CREATE TABLE tasks_organization (
        id SERIAL PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        slug VARCHAR(100) UNIQUE NOT NULL,
        owner_id INTEGER NOT NULL,
        plan VARCHAR(50) DEFAULT 'free',
        is_active BOOLEAN DEFAULT TRUE,
        ...
    );

    -- Organization memberships
    CREATE TABLE tasks_organization_membership (
        id SERIAL PRIMARY KEY,
        organization_id INTEGER REFERENCES tasks_organization(id),
        user_id INTEGER,  -- NULL for pending invitations
        user_email VARCHAR(254),
        role VARCHAR(20) DEFAULT 'member',
        is_pending BOOLEAN DEFAULT FALSE,
        ...
    );

    -- Projects
    CREATE TABLE tasks_project (
        id SERIAL PRIMARY KEY,
        organization_id INTEGER REFERENCES tasks_organization(id),
        name VARCHAR(255) NOT NULL,
        key VARCHAR(10) NOT NULL,
        access_level VARCHAR(20) DEFAULT 'limited',
        next_item_number INTEGER DEFAULT 1,
        ...
        UNIQUE (organization_id, key)
    );

    -- Project memberships
    CREATE TABLE tasks_project_membership (
        id SERIAL PRIMARY KEY,
        project_id INTEGER REFERENCES tasks_project(id),
        user_id INTEGER NOT NULL,
        role VARCHAR(20) DEFAULT 'member',
        can_browse BOOLEAN DEFAULT TRUE,
        can_create BOOLEAN DEFAULT TRUE,
        ...
        UNIQUE (project_id, user_id)
    );
