============
Comments API
============

Comments allow users to discuss and collaborate on items (tasks, actions, alerts)
within projects, similar to Jira issue comments.

Overview
--------

Comments provide:

- Threaded discussions on any item
- Author tracking with display names
- Edit/delete capabilities for own comments
- Access control based on item visibility

Endpoints
---------

List Comments
^^^^^^^^^^^^^

.. code-block:: http

    GET /api/comments/?item={item_id}

Returns comments for a specific item.

**Query Parameters:**

- ``item`` (int, required): The item ID to get comments for

**Response:**

.. code-block:: json

    {
        "count": 3,
        "next": null,
        "previous": null,
        "results": [
            {
                "id": 1,
                "item": 42,
                "author_id": 5,
                "author_email": "user@example.com",
                "author_name": "John Doe",
                "author_display_name": "John Doe",
                "content": "This needs to be prioritized for the next sprint.",
                "parent": null,
                "created_at": "2026-01-28T14:30:00Z",
                "updated_at": "2026-01-28T14:30:00Z",
                "is_edited": false,
                "reply_count": 1
            }
        ]
    }

**Notes:**

- Users can only see comments on items they have access to
- For personal items (no project), only the owner can see comments
- For project items, access is based on project membership

Create Comment
^^^^^^^^^^^^^^

.. code-block:: http

    POST /api/comments/

**Request Body:**

.. code-block:: json

    {
        "item": 42,
        "content": "This looks good, but we should add tests.",
        "parent": null
    }

**Notes:**

- ``item`` must be an item the user has access to
- ``parent`` is optional, used for threaded replies
- Author info is automatically set from the authenticated user

**Response:**

.. code-block:: json

    {
        "id": 2,
        "item": 42,
        "author_id": 5,
        "author_email": "user@example.com",
        "author_name": "John Doe",
        "author_display_name": "John Doe",
        "content": "This looks good, but we should add tests.",
        "parent": null,
        "created_at": "2026-01-28T15:00:00Z",
        "updated_at": "2026-01-28T15:00:00Z",
        "is_edited": false,
        "reply_count": 0
    }

Update Comment
^^^^^^^^^^^^^^

.. code-block:: http

    PATCH /api/comments/{id}/

**Permissions:** Only the comment author can edit their own comments

**Request Body:**

.. code-block:: json

    {
        "content": "Updated comment text with more details."
    }

**Notes:**

- Only ``content`` can be updated
- ``is_edited`` is automatically set to ``true`` after update

Delete Comment
^^^^^^^^^^^^^^

.. code-block:: http

    DELETE /api/comments/{id}/

**Permissions:** Only the comment author can delete their own comments

**Response:** ``204 No Content``

Threading
---------

Comments support simple threading through the ``parent`` field:

.. code-block:: json

    {
        "item": 42,
        "content": "I agree with the above point.",
        "parent": 1
    }

The ``reply_count`` field shows how many direct replies a comment has.

Error Responses
---------------

**403 Forbidden (No access to item):**

.. code-block:: json

    {
        "detail": "You don't have access to comment on this item"
    }

**403 Forbidden (Not comment author):**

.. code-block:: json

    {
        "detail": "You can only edit your own comments"
    }

**400 Bad Request (Parent comment mismatch):**

.. code-block:: json

    {
        "parent": ["Parent comment must belong to the same item"]
    }
