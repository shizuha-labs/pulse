"""
Exhaustive E2E tests for Phase 5: Issue Types, Issue Type Schemes, and related features.
"""
from django.test import override_settings
from rest_framework import status as http_status
from tasks.models import (
    IssueType, IssueTypeScheme, IssueTypeSchemeMapping,
    Project,
)
from .base import AuthenticatedAPITestCase


class IssueTypeAPITests(AuthenticatedAPITestCase):
    """Full CRUD + edge case tests for IssueType API."""

    def test_list_seeded_issue_types(self):
        """Verify the 7 seeded issue types exist."""
        resp = self.client.get('/shizuha-pulse/api/issue-types/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        data = resp.json()
        results = data if isinstance(data, list) else data.get('results', data)
        slugs = [it['slug'] for it in results]
        for expected in ['epic', 'story', 'bug', 'task', 'subtask', 'improvement', 'new_feature']:
            self.assertIn(expected, slugs, f"Missing seeded issue type: {expected}")

    def test_seeded_hierarchy_levels(self):
        """Verify hierarchy levels: Epic=0, Story/Bug=1, Task=2, Sub-task=3."""
        resp = self.client.get('/shizuha-pulse/api/issue-types/')
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        levels = {it['slug']: it['hierarchy_level'] for it in results}
        self.assertEqual(levels['epic'], 0)
        self.assertEqual(levels['story'], 1)
        self.assertEqual(levels['bug'], 1)
        self.assertEqual(levels['task'], 2)
        self.assertEqual(levels['subtask'], 3)

    def test_seeded_subtask_flag(self):
        """Only subtask type should have is_subtask=True."""
        resp = self.client.get('/shizuha-pulse/api/issue-types/')
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        for it in results:
            if it['slug'] == 'subtask':
                self.assertTrue(it['is_subtask'])
            else:
                self.assertFalse(it['is_subtask'], f"{it['slug']} should not be subtask")

    def test_create_custom_issue_type(self):
        """Create a new custom issue type via API."""
        resp = self.client.post('/shizuha-pulse/api/issue-types/', {
            'name': 'Spike',
            'slug': 'spike',
            'item_type_prefix': 'task.spike',
            'hierarchy_level': 1,
            'icon': 'zap',
            'color': '#a855f7',
            'is_subtask': False,
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['name'], 'Spike')
        self.assertEqual(resp.json()['slug'], 'spike')
        self.assertTrue(IssueType.objects.filter(slug='spike').exists())

    def test_create_duplicate_slug_fails(self):
        """Duplicate slug should be rejected."""
        resp = self.client.post('/shizuha-pulse/api/issue-types/', {
            'name': 'Another Bug',
            'slug': 'bug',  # already exists
            'item_type_prefix': 'task.anotherbug',
            'hierarchy_level': 1,
        })
        self.assertEqual(resp.status_code, http_status.HTTP_400_BAD_REQUEST)

    def test_retrieve_issue_type(self):
        """Get a specific issue type by ID."""
        it = IssueType.objects.get(slug='epic')
        resp = self.client.get(f'/shizuha-pulse/api/issue-types/{it.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertEqual(resp.json()['name'], 'Epic')
        self.assertEqual(resp.json()['color'], '#8b5cf6')

    def test_update_issue_type(self):
        """Patch an issue type."""
        it = IssueType.objects.create(
            name='Temp', slug='temp-type', item_type_prefix='task.temp',
            hierarchy_level=1
        )
        resp = self.client.patch(f'/shizuha-pulse/api/issue-types/{it.id}/', {
            'name': 'Updated Temp',
            'color': '#ff0000',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        it.refresh_from_db()
        self.assertEqual(it.name, 'Updated Temp')
        self.assertEqual(it.color, '#ff0000')

    def test_delete_issue_type(self):
        """Delete a non-system issue type."""
        it = IssueType.objects.create(
            name='Deleteme', slug='deleteme', item_type_prefix='task.deleteme',
            hierarchy_level=1
        )
        resp = self.client.delete(f'/shizuha-pulse/api/issue-types/{it.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)
        self.assertFalse(IssueType.objects.filter(slug='deleteme').exists())

    def test_item_type_prefix_mapping(self):
        """Verify item_type_prefix for all seeded types matches expected patterns."""
        expected = {
            'epic': 'task.epic',
            'story': 'task.story',
            'bug': 'task.bug',
            'task': 'task.generic',
            'subtask': 'task.subtask',
            'improvement': 'task.improvement',
            'new_feature': 'task.feature',
        }
        for slug, prefix in expected.items():
            it = IssueType.objects.get(slug=slug)
            self.assertEqual(it.item_type_prefix, prefix)


class IssueTypeSchemeAPITests(AuthenticatedAPITestCase):
    """Full CRUD + mapping tests for IssueTypeScheme API."""

    def test_list_seeded_scheme(self):
        """Default scheme should exist with all 7 issue types."""
        resp = self.client.get('/shizuha-pulse/api/issue-type-schemes/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        self.assertTrue(len(results) >= 1)
        default = [s for s in results if s['slug'] == 'default']
        self.assertEqual(len(default), 1)

    def test_default_scheme_has_all_types(self):
        """Default scheme should map all 7 issue types."""
        scheme = IssueTypeScheme.objects.get(slug='default')
        self.assertEqual(scheme.mappings.count(), 7)

    def test_default_issue_type_is_task(self):
        """Default issue type should be 'Task'."""
        scheme = IssueTypeScheme.objects.get(slug='default')
        self.assertIsNotNone(scheme.default_issue_type)
        self.assertEqual(scheme.default_issue_type.slug, 'task')

    def test_create_custom_scheme(self):
        """Create a new issue type scheme with selected types."""
        resp = self.client.post('/shizuha-pulse/api/issue-type-schemes/', {
            'name': 'Minimal Scheme',
            'slug': 'minimal',
            'description': 'Only tasks and bugs',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        scheme_id = resp.json()['id']
        self.assertTrue(IssueTypeScheme.objects.filter(slug='minimal').exists())

    def test_add_mapping_to_scheme(self):
        """Add an issue type mapping to a scheme."""
        scheme = IssueTypeScheme.objects.create(name='Test Scheme', slug='test-mapping-scheme')
        task_type = IssueType.objects.get(slug='task')
        resp = self.client.post(
            f'/shizuha-pulse/api/issue-type-schemes/{scheme.id}/add_mapping/',
            {'issue_type': str(task_type.id), 'position': 0}
        )
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(scheme.mappings.count(), 1)
        self.assertEqual(scheme.mappings.first().issue_type, task_type)

    def test_add_duplicate_mapping_fails(self):
        """Adding the same issue type twice should fail."""
        scheme = IssueTypeScheme.objects.create(name='Dup Scheme', slug='dup-scheme')
        task_type = IssueType.objects.get(slug='task')
        IssueTypeSchemeMapping.objects.create(
            scheme=scheme, issue_type=task_type, position=0
        )
        resp = self.client.post(
            f'/shizuha-pulse/api/issue-type-schemes/{scheme.id}/add_mapping/',
            {'issue_type': str(task_type.id), 'position': 1}
        )
        self.assertIn(resp.status_code, [http_status.HTTP_400_BAD_REQUEST, http_status.HTTP_409_CONFLICT])

    def test_remove_mapping_from_scheme(self):
        """Remove an issue type mapping from a scheme."""
        scheme = IssueTypeScheme.objects.create(name='Remove Scheme', slug='remove-scheme')
        task_type = IssueType.objects.get(slug='task')
        IssueTypeSchemeMapping.objects.create(
            scheme=scheme, issue_type=task_type, position=0
        )
        resp = self.client.post(
            f'/shizuha-pulse/api/issue-type-schemes/{scheme.id}/remove_mapping/',
            {'issue_type': str(task_type.id)}
        )
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        self.assertEqual(scheme.mappings.count(), 0)

    def test_retrieve_scheme_detail(self):
        """Get scheme with nested mappings."""
        scheme = IssueTypeScheme.objects.get(slug='default')
        resp = self.client.get(f'/shizuha-pulse/api/issue-type-schemes/{scheme.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(data['slug'], 'default')

    def test_delete_scheme(self):
        """Delete a non-system scheme."""
        scheme = IssueTypeScheme.objects.create(
            name='Delete Me', slug='delete-me-scheme'
        )
        resp = self.client.delete(f'/shizuha-pulse/api/issue-type-schemes/{scheme.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)
        self.assertFalse(IssueTypeScheme.objects.filter(slug='delete-me-scheme').exists())

    def test_update_scheme(self):
        """Update scheme name and description."""
        scheme = IssueTypeScheme.objects.create(
            name='Old Name', slug='update-scheme'
        )
        resp = self.client.patch(f'/shizuha-pulse/api/issue-type-schemes/{scheme.id}/', {
            'name': 'New Name',
            'description': 'Updated description',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        scheme.refresh_from_db()
        self.assertEqual(scheme.name, 'New Name')
