"""
Exhaustive E2E tests for Phase 5: Screens, Screen Schemes, Issue Type Screen Schemes.
"""
from rest_framework import status as http_status
from tasks.models import (
    Screen, ScreenField, ScreenScheme,
    IssueTypeScreenScheme, IssueTypeScreenSchemeMapping,
    CustomField, IssueType,
)
from .base import AuthenticatedAPITestCase


class ScreenAPITests(AuthenticatedAPITestCase):
    """CRUD tests for Screen API."""

    def test_list_seeded_screens(self):
        """Verify 3 default screens exist."""
        resp = self.client.get('/shizuha-pulse/api/screens/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        slugs = [s['slug'] for s in results]
        self.assertIn('default-create', slugs)
        self.assertIn('default-edit', slugs)
        self.assertIn('default-view', slugs)

    def test_create_screen_with_tabs(self):
        resp = self.client.post('/shizuha-pulse/api/screens/', {
            'name': 'Bug Create Screen',
            'slug': 'bug-create',
            'tabs': [
                {'name': 'Details', 'position': 0},
                {'name': 'Environment', 'position': 1},
                {'name': 'Reproduction', 'position': 2},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(len(resp.json()['tabs']), 3)

    def test_create_screen_with_fields(self):
        """Create a screen, then add fields to it."""
        screen = Screen.objects.create(
            name='Test Screen', slug='test-screen-fields'
        )
        # Add built-in fields
        ScreenField.objects.create(
            screen=screen, field_name='summary',
            tab_name='Details', position=0, is_required=True
        )
        ScreenField.objects.create(
            screen=screen, field_name='priority',
            tab_name='Details', position=1, is_required=False
        )
        ScreenField.objects.create(
            screen=screen, field_name='assignee',
            tab_name='Details', position=2, is_required=False
        )
        # Add custom field
        cf = CustomField.objects.create(
            name='Story Points', slug='sp-screen', field_type='number'
        )
        ScreenField.objects.create(
            screen=screen, field_name='sp-screen',
            custom_field=cf, tab_name='Details', position=3
        )
        self.assertEqual(screen.fields.count(), 4)

        # Verify via API
        resp = self.client.get(f'/shizuha-pulse/api/screens/{screen.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

    def test_update_screen(self):
        screen = Screen.objects.create(name='Update Me', slug='update-screen')
        resp = self.client.patch(f'/shizuha-pulse/api/screens/{screen.id}/', {
            'name': 'Updated Screen',
            'tabs': [{'name': 'Main', 'position': 0}],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        screen.refresh_from_db()
        self.assertEqual(screen.name, 'Updated Screen')

    def test_delete_screen(self):
        screen = Screen.objects.create(name='Delete Me', slug='delete-screen')
        resp = self.client.delete(f'/shizuha-pulse/api/screens/{screen.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)


class ScreenSchemeAPITests(AuthenticatedAPITestCase):
    """CRUD tests for ScreenScheme API."""

    def test_list_seeded_screen_scheme(self):
        resp = self.client.get('/shizuha-pulse/api/screen-schemes/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        slugs = [s['slug'] for s in results]
        self.assertIn('default', slugs)

    def test_default_scheme_has_screens(self):
        """Default screen scheme should link to all three default screens."""
        scheme = ScreenScheme.objects.get(slug='default')
        self.assertIsNotNone(scheme.create_screen)
        self.assertIsNotNone(scheme.edit_screen)
        self.assertIsNotNone(scheme.view_screen)

    def test_create_screen_scheme(self):
        create_screen = Screen.objects.create(name='Custom Create', slug='custom-create-ss')
        edit_screen = Screen.objects.create(name='Custom Edit', slug='custom-edit-ss')
        resp = self.client.post('/shizuha-pulse/api/screen-schemes/', {
            'name': 'Custom Scheme',
            'slug': 'custom-scheme',
            'create_screen': str(create_screen.id),
            'edit_screen': str(edit_screen.id),
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_update_screen_scheme(self):
        scheme = ScreenScheme.objects.create(name='Upd', slug='upd-ss')
        new_screen = Screen.objects.create(name='New View', slug='new-view-ss')
        resp = self.client.patch(f'/shizuha-pulse/api/screen-schemes/{scheme.id}/', {
            'view_screen': str(new_screen.id),
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        scheme.refresh_from_db()
        self.assertEqual(scheme.view_screen, new_screen)

    def test_delete_screen_scheme(self):
        scheme = ScreenScheme.objects.create(name='Del', slug='del-ss')
        resp = self.client.delete(f'/shizuha-pulse/api/screen-schemes/{scheme.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)


class IssueTypeScreenSchemeAPITests(AuthenticatedAPITestCase):
    """Tests for IssueTypeScreenScheme — maps issue types to screen schemes."""

    def test_list_seeded_itss(self):
        resp = self.client.get('/shizuha-pulse/api/issue-type-screen-schemes/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        slugs = [s['slug'] for s in results]
        self.assertIn('default', slugs)

    def test_create_itss_with_mappings(self):
        """Create an issue type screen scheme and add a mapping."""
        default_ss = ScreenScheme.objects.get(slug='default')
        bug_ss = ScreenScheme.objects.create(name='Bug SS', slug='bug-ss-itss')

        itss = IssueTypeScreenScheme.objects.create(
            name='Custom ITSS', slug='custom-itss',
            default_screen_scheme=default_ss
        )
        bug_type = IssueType.objects.get(slug='bug')
        IssueTypeScreenSchemeMapping.objects.create(
            scheme=itss, issue_type=bug_type, screen_scheme=bug_ss
        )

        resp = self.client.get(f'/shizuha-pulse/api/issue-type-screen-schemes/{itss.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

    def test_delete_itss(self):
        default_ss = ScreenScheme.objects.get(slug='default')
        itss = IssueTypeScreenScheme.objects.create(
            name='Del ITSS', slug='del-itss',
            default_screen_scheme=default_ss
        )
        resp = self.client.delete(f'/shizuha-pulse/api/issue-type-screen-schemes/{itss.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)
