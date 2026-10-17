=======
Testing
=======

Testing guide for Shizuha Hub.

.. contents:: On This Page
   :local:
   :depth: 2

Test Structure
--------------

Tests are organized in ``tasks/tests/``:

.. code-block:: text

    tasks/tests/
    ├── __init__.py
    ├── test_models.py       # Model tests
    ├── test_views.py        # API endpoint tests
    ├── test_services.py     # Service tests
    └── test_integration.py  # End-to-end tests

Running Tests
-------------

All Tests
^^^^^^^^^

.. code-block:: bash

    docker-compose exec hub python manage.py test tasks.tests

Specific Module
^^^^^^^^^^^^^^^

.. code-block:: bash

    # Models only
    docker-compose exec hub python manage.py test tasks.tests.test_models

    # Services only
    docker-compose exec hub python manage.py test tasks.tests.test_services

Specific Test Class
^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    docker-compose exec hub python manage.py test tasks.tests.test_models.ItemModelTests

With Verbosity
^^^^^^^^^^^^^^

.. code-block:: bash

    # Verbose output
    docker-compose exec hub python manage.py test tasks.tests -v 2

    # Very verbose
    docker-compose exec hub python manage.py test tasks.tests -v 3

Test Categories
---------------

Model Tests
^^^^^^^^^^^

Test model creation, validation, and methods:

.. code-block:: python

    from django.test import TestCase
    from tasks.models import Item

    class ItemModelTests(TestCase):
        def test_create_task(self):
            task = Item.objects.create(
                mode='task',
                title='Test Task'
            )
            self.assertEqual(task.mode, 'task')
            self.assertEqual(task.status, 'pending')

        def test_mark_completed(self):
            task = Item.objects.create(mode='task', title='Test')
            task.mark_completed()
            self.assertEqual(task.status, 'completed')
            self.assertIsNotNone(task.completed_at)

Service Tests
^^^^^^^^^^^^^

Test business logic in services:

.. code-block:: python

    from django.test import TestCase
    from tasks.services import AlertDeduplicationService
    from tasks.models import Item

    class DeduplicationServiceTests(TestCase):
        def test_submit_new_alert(self):
            data = {
                'title': 'Test Alert',
                'source': 'test',
                'labels': {'host': 'web-01'}
            }
            alert, is_new = AlertDeduplicationService.submit_alert(data)
            self.assertTrue(is_new)
            self.assertIsNotNone(alert.fingerprint)

        def test_deduplicate_same_alert(self):
            data = {'title': 'Alert', 'source': 'test', 'labels': {}}

            alert1, _ = AlertDeduplicationService.submit_alert(data)
            alert2, is_new = AlertDeduplicationService.submit_alert(data)

            self.assertFalse(is_new)
            self.assertEqual(alert1.id, alert2.id)
            self.assertEqual(alert2.occurrence_count, 2)

API Tests
^^^^^^^^^

Test REST endpoints:

.. code-block:: python

    from rest_framework.test import APITestCase
    from rest_framework import status
    from tasks.models import Item

    class ItemAPITests(APITestCase):
        def test_create_task(self):
            response = self.client.post('/api/items/', {
                'mode': 'task',
                'title': 'API Task',
                'priority': 'high'
            }, format='json')

            self.assertEqual(response.status_code, status.HTTP_201_CREATED)
            self.assertEqual(response.data['mode'], 'task')

        def test_list_alerts(self):
            Item.objects.create(mode='alert', title='Alert 1', status='firing')
            Item.objects.create(mode='alert', title='Alert 2', status='firing')

            response = self.client.get('/api/items/alerts/')

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data['count'], 2)

Integration Tests
^^^^^^^^^^^^^^^^^

Test complete workflows:

.. code-block:: python

    from django.test import TransactionTestCase
    from rest_framework.test import APIClient
    from tasks.models import Item

    class AlertWorkflowTests(TransactionTestCase):
        def setUp(self):
            self.client = APIClient()

        def test_alert_lifecycle(self):
            # 1. Submit alert
            response = self.client.post('/api/alerts/', {
                'title': 'High CPU',
                'source': 'prometheus',
                'severity': 'warning'
            }, format='json')
            alert_id = response.data['item_id']

            # 2. Acknowledge
            self.client.post(f'/api/items/{alert_id}/acknowledge/')

            # 3. Resolve
            self.client.post(f'/api/items/{alert_id}/resolve/', {
                'note': 'Fixed'
            }, format='json')

            # 4. Verify
            alert = Item.objects.get(id=alert_id)
            self.assertEqual(alert.status, 'resolved')

Test Fixtures
-------------

Using Fixtures
^^^^^^^^^^^^^^

.. code-block:: python

    class MyTests(TestCase):
        fixtures = ['test_data.json']

        def test_with_fixture_data(self):
            # Data from fixture is available
            items = Item.objects.all()
            self.assertGreater(items.count(), 0)

Creating Fixtures
^^^^^^^^^^^^^^^^^

.. code-block:: bash

    # Dump existing data
    docker-compose exec hub python manage.py dumpdata tasks --indent 2 > fixtures/test_data.json

Factory Pattern
^^^^^^^^^^^^^^^

.. code-block:: python

    class ItemFactory:
        @staticmethod
        def create_task(**kwargs):
            defaults = {
                'mode': 'task',
                'title': 'Test Task',
                'status': 'pending'
            }
            defaults.update(kwargs)
            return Item.objects.create(**defaults)

        @staticmethod
        def create_alert(**kwargs):
            defaults = {
                'mode': 'alert',
                'title': 'Test Alert',
                'status': 'firing',
                'source': 'test'
            }
            defaults.update(kwargs)
            return Item.objects.create(**defaults)

    # Usage
    class MyTests(TestCase):
        def test_something(self):
            task = ItemFactory.create_task(priority='high')
            alert = ItemFactory.create_alert(severity='critical')

Coverage
--------

Run with Coverage
^^^^^^^^^^^^^^^^^

.. code-block:: bash

    # Install coverage
    pip install coverage

    # Run with coverage
    coverage run --source='tasks' manage.py test tasks.tests

    # Generate report
    coverage report

    # HTML report
    coverage html

Coverage Configuration
^^^^^^^^^^^^^^^^^^^^^^

Create ``.coveragerc``:

.. code-block:: ini

    [run]
    source = tasks
    omit =
        */migrations/*
        */tests/*
        */__init__.py

    [report]
    exclude_lines =
        pragma: no cover
        def __repr__
        raise NotImplementedError

Best Practices
--------------

1. **One assertion per test** - Makes failures clear
2. **Descriptive names** - ``test_approve_action_sets_approved_at``
3. **Test edge cases** - Empty inputs, duplicates, errors
4. **Use setUp/tearDown** - For common setup/cleanup
5. **Isolate tests** - Each test should be independent
6. **Mock external services** - Don't call real APIs in tests
