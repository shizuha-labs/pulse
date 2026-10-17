===================
Permission Service
===================

The ``PermissionService`` handles authorization for organizations, projects, and items,
providing a centralized way to check user access and retrieve filtered querysets.

Overview
--------

.. mermaid::

    flowchart TB
        subgraph "Permission Check Flow"
            REQ[User Request]
            ORG[Check Org Membership]
            PROJ[Check Project Membership]
            ACCESS[Check Access Level]
            PERM[Check Granular Permission]
        end

        REQ --> ORG
        ORG --> |member| PROJ
        PROJ --> ACCESS
        ACCESS --> PERM
        PERM --> ALLOW[Allow/Deny]

Usage
-----

Getting User Organizations
^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    from tasks.services.permissions import PermissionService

    # Get all organizations a user belongs to
    orgs = PermissionService.get_user_organizations(user_id=1)

    # Returns queryset of Organization objects where:
    # - User has an active membership (is_active=True)
    # - Membership is not pending (is_pending=False)

Getting User Projects
^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    # Get all projects a user can access
    projects = PermissionService.get_user_projects(user_id=1)

    # Filter by organization
    projects = PermissionService.get_user_projects(
        user_id=1,
        organization_id=5
    )

    # Returns projects where:
    # - User is a direct project member, OR
    # - Project has access_level='open' and user is in the organization

Getting User Item Queryset
^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    # Get all items a user can access (personal + project items)
    items = PermissionService.get_user_item_queryset(user)

    # Exclude personal items (only project items)
    items = PermissionService.get_user_item_queryset(
        user,
        include_personal=False
    )

    # Filter to a specific project
    items = PermissionService.get_user_item_queryset(
        user,
        project_id=1
    )

Checking Project Permissions
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

    # Check if user can perform an action in a project
    can_create = PermissionService.can_user_create_in_project(
        user_id=1,
        project=project
    )

    can_edit = PermissionService.can_user_edit_in_project(
        user_id=1,
        project=project
    )

    can_delete = PermissionService.can_user_delete_in_project(
        user_id=1,
        project=project
    )

Permission Logic
----------------

Organization Access
^^^^^^^^^^^^^^^^^^^

Users can access an organization if they have an **active, non-pending membership**.

.. code-block:: python

    # User has access if:
    OrganizationMembership.objects.filter(
        organization=org,
        user_id=user_id,
        is_active=True,
        is_pending=False
    ).exists()

Project Access
^^^^^^^^^^^^^^

Project access depends on the ``access_level`` setting:

.. list-table::
   :widths: 20 40 40
   :header-rows: 1

   * - Access Level
     - Who Can View
     - Who Can Create/Edit
   * - **Open**
     - All org members
     - All org members
   * - **Limited**
     - All org members
     - Project members only
   * - **Private**
     - Project members only
     - Project members only

Item Access
^^^^^^^^^^^

**Personal Items** (no project):
   - Only the creator or assignee can access

**Project Items**:
   - Access based on project membership and permissions
   - Staff users have full access to everything

Granular Permissions
^^^^^^^^^^^^^^^^^^^^

Project members have granular permissions that control specific actions:

.. code-block:: python

    class ProjectMembership:
        can_browse = True      # View project and items
        can_create = True      # Create new items
        can_edit = True        # Edit existing items
        can_delete = False     # Delete items
        can_manage_project = False    # Update project settings
        can_manage_members = False    # Add/remove members

These are set based on the member's role but can be customized per member.

Integration with Views
----------------------

The permission service is used in ViewSets to filter querysets:

.. code-block:: python

    class ItemViewSet(viewsets.ModelViewSet):
        def get_queryset(self):
            user = self.request.user

            # Staff sees everything
            if user.is_staff:
                return Item.objects.all()

            # Regular users see filtered items
            return PermissionService.get_user_item_queryset(
                user,
                include_personal=True,
                project_id=self.request.query_params.get('project')
            )

Permission Classes
------------------

The ``ItemProjectPermission`` class provides object-level permission checks:

.. code-block:: python

    from tasks.permissions import ItemProjectPermission

    class ItemViewSet(viewsets.ModelViewSet):
        permission_classes = [IsAuthenticated, ItemProjectPermission]

This ensures:

1. **Read access**: Checked via project membership or personal ownership
2. **Write access**: Checked via ``can_edit`` permission
3. **Delete access**: Checked via ``can_delete`` permission or ownership

Error Handling
--------------

The service returns empty querysets for invalid users rather than raising exceptions:

.. code-block:: python

    # Anonymous user returns empty queryset
    items = PermissionService.get_user_item_queryset(AnonymousUser())
    assert items.count() == 0

    # User with no memberships returns only personal items
    items = PermissionService.get_user_item_queryset(new_user)
    # Returns items where new_user is creator or assignee
