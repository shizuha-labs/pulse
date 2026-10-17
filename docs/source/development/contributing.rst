============
Contributing
============

Guidelines for contributing to Shizuha Hub.

.. contents:: On This Page
   :local:
   :depth: 2

Development Setup
-----------------

1. Clone the repository:

   .. code-block:: bash

       git clone https://github.com/shizuha-stack/shizuha-pulse.git
       cd cli-pulse

2. Start development environment:

   .. code-block:: bash

       docker-compose -f docker-compose.dev.yml up --build

3. The code is mounted as a volume, so changes are reflected immediately.

Code Style
----------

Python
^^^^^^

- Follow PEP 8 style guide
- Use type hints where beneficial
- Maximum line length: 120 characters
- Use meaningful variable names

Docstrings
^^^^^^^^^^

Use Google-style docstrings:

.. code-block:: python

    def submit_alert(data: dict) -> tuple[Item, bool]:
        """
        Submit an alert with automatic deduplication.

        Args:
            data: Alert data containing:
                - title (required): Alert title
                - source (required): Source identifier
                - labels (optional): Key-value labels

        Returns:
            Tuple of (alert, is_new) where is_new indicates
            if this is a new alert or deduplicated.

        Raises:
            ValidationError: If required fields are missing.

        Example:
            >>> alert, is_new = submit_alert({
            ...     'title': 'High CPU',
            ...     'source': 'prometheus'
            ... })
        """

Testing
-------

Running Tests
^^^^^^^^^^^^^

.. code-block:: bash

    # Run all tests
    docker-compose exec hub python manage.py test tasks.tests

    # Run specific test module
    docker-compose exec hub python manage.py test tasks.tests.test_models

    # Run with verbosity
    docker-compose exec hub python manage.py test tasks.tests -v 2

Writing Tests
^^^^^^^^^^^^^

- Place tests in ``tasks/tests/``
- Use descriptive test names
- One assertion per test when possible
- Test both success and error cases

.. code-block:: python

    from django.test import TestCase
    from tasks.models import Item

    class ItemModelTests(TestCase):
        """Test Item model functionality."""

        def test_create_task_with_defaults(self):
            """Test creating a task uses correct defaults."""
            task = Item.objects.create(
                mode='task',
                title='Test Task'
            )
            self.assertEqual(task.status, 'pending')
            self.assertEqual(task.priority, 'normal')

        def test_complete_task_sets_completed_at(self):
            """Test completing a task sets the timestamp."""
            task = Item.objects.create(mode='task', title='Test')
            task.mark_completed()
            self.assertIsNotNone(task.completed_at)

Pull Request Process
--------------------

1. Create a feature branch:

   .. code-block:: bash

       git checkout -b feature/my-new-feature

2. Make your changes with tests

3. Run tests locally:

   .. code-block:: bash

       docker-compose exec hub python manage.py test tasks.tests

4. Commit with descriptive message:

   .. code-block:: bash

       git commit -m "Add support for custom fingerprint fields"

5. Push and create PR:

   .. code-block:: bash

       git push origin feature/my-new-feature

Documentation
-------------

Building Docs
^^^^^^^^^^^^^

.. code-block:: bash

    # Build documentation
    docker-compose exec docs make html

    # Or with live reload
    docker-compose up docs

Updating Docs
^^^^^^^^^^^^^

- API docs are auto-generated from docstrings
- Update RST files in ``docs/source/`` for guides
- Add new pages to the appropriate ``toctree``

Release Process
---------------

1. Update version in ``setup.py`` and ``docs/source/conf.py``
2. Update CHANGELOG
3. Create release tag:

   .. code-block:: bash

       git tag -a v1.0.0 -m "Release 1.0.0"
       git push origin v1.0.0

4. Build and push Docker image:

   .. code-block:: bash

       docker build -t shizuha/shizuha-pulse:1.0.0 .
       docker push shizuha/shizuha-pulse:1.0.0
