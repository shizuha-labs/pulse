"""
Exhaustive E2E tests for Phase 5: Custom Fields, Contexts, and Values.
"""
import json
from rest_framework import status as http_status
from tasks.models import (
    CustomField, CustomFieldContext, CustomFieldValue,
    IssueType, Project, Item,
)
from .base import AuthenticatedAPITestCase


class CustomFieldAPITests(AuthenticatedAPITestCase):
    """CRUD tests for CustomField API — all 20 field types."""

    def test_create_text_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Customer Name',
            'slug': 'customer_name',
            'field_type': 'text',
            'max_length': 200,
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['field_type'], 'text')
        self.assertEqual(resp.json()['max_length'], 200)

    def test_create_textarea_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Root Cause',
            'slug': 'root_cause',
            'field_type': 'textarea',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_number_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Story Points',
            'slug': 'story_points',
            'field_type': 'number',
            'min_value': 0,
            'max_value': 100,
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['min_value'], 0.0)
        self.assertEqual(resp.json()['max_value'], 100.0)

    def test_create_select_field_with_options(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Environment',
            'slug': 'environment',
            'field_type': 'select',
            'options': [
                {'value': 'dev', 'label': 'Development'},
                {'value': 'staging', 'label': 'Staging'},
                {'value': 'prod', 'label': 'Production'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(len(resp.json()['options']), 3)

    def test_create_multiselect_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Affected Versions',
            'slug': 'affected_versions',
            'field_type': 'multiselect',
            'options': [
                {'value': 'v1.0', 'label': '1.0'},
                {'value': 'v2.0', 'label': '2.0'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_cascading_select_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Location',
            'slug': 'location',
            'field_type': 'cascading_select',
            'options': [
                {'value': 'us', 'label': 'US', 'children': [
                    {'value': 'ny', 'label': 'New York'},
                    {'value': 'ca', 'label': 'California'},
                ]},
                {'value': 'in', 'label': 'India', 'children': [
                    {'value': 'mh', 'label': 'Maharashtra'},
                ]},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['field_type'], 'cascading_select')

    def test_create_checkbox_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Components Affected',
            'slug': 'components_affected',
            'field_type': 'checkbox',
            'options': [
                {'value': 'frontend', 'label': 'Frontend'},
                {'value': 'backend', 'label': 'Backend'},
                {'value': 'mobile', 'label': 'Mobile'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_date_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Target Release Date',
            'slug': 'target_release_date',
            'field_type': 'date',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_datetime_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Incident Start Time',
            'slug': 'incident_start_time',
            'field_type': 'datetime',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_user_picker_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'QA Owner',
            'slug': 'qa_owner',
            'field_type': 'user',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_multiuser_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Reviewers',
            'slug': 'reviewers',
            'field_type': 'multiuser',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_url_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Design Doc URL',
            'slug': 'design_doc_url',
            'field_type': 'url',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_labels_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Custom Labels',
            'slug': 'custom_labels',
            'field_type': 'labels',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_readonly_field(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Auto-Generated ID',
            'slug': 'auto_gen_id',
            'field_type': 'readonly',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_field_with_regex_validation(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Ticket ID',
            'slug': 'ticket_id',
            'field_type': 'text',
            'validation_regex': r'^TICKET-\d+$',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['validation_regex'], r'^TICKET-\d+$')

    def test_create_field_with_default_value(self):
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Priority Score',
            'slug': 'priority_score',
            'field_type': 'number',
            'default_value': 5,
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['default_value'], 5)

    def test_duplicate_slug_fails(self):
        CustomField.objects.create(
            name='Existing', slug='existing-cf', field_type='text'
        )
        resp = self.client.post('/shizuha-pulse/api/custom-fields/', {
            'name': 'Another',
            'slug': 'existing-cf',
            'field_type': 'text',
        })
        self.assertEqual(resp.status_code, http_status.HTTP_400_BAD_REQUEST)

    def test_list_custom_fields(self):
        for i in range(3):
            CustomField.objects.create(
                name=f'Field {i}', slug=f'field-{i}', field_type='text'
            )
        resp = self.client.get('/shizuha-pulse/api/custom-fields/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        self.assertGreaterEqual(len(results), 3)

    def test_update_custom_field(self):
        cf = CustomField.objects.create(
            name='Old Name', slug='update-cf', field_type='text'
        )
        resp = self.client.patch(f'/shizuha-pulse/api/custom-fields/{cf.id}/', {
            'name': 'New Name',
            'is_searchable': False,
        })
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        cf.refresh_from_db()
        self.assertEqual(cf.name, 'New Name')
        self.assertFalse(cf.is_searchable)

    def test_delete_custom_field(self):
        cf = CustomField.objects.create(
            name='Delete Me', slug='delete-cf', field_type='text'
        )
        resp = self.client.delete(f'/shizuha-pulse/api/custom-fields/{cf.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)


class CustomFieldContextAPITests(AuthenticatedAPITestCase):
    """Tests for CustomFieldContext API — scoping fields to projects/issue types."""

    def setUp(self):
        super().setUp()
        self.field = CustomField.objects.create(
            name='Sprint Goal', slug='sprint-goal-ctx', field_type='text'
        )
        self.project = Project.objects.create(
            name='Context Project', key='CTX', org_id=1
        )

    def test_create_global_context(self):
        """Create a context with no project/issue type restrictions (applies to all)."""
        resp = self.client.post('/shizuha-pulse/api/custom-field-contexts/', {
            'custom_field': str(self.field.id),
            'name': 'Global Context',
            'visibility': 'visible',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_create_project_scoped_context(self):
        """Create a context scoped to a specific project."""
        resp = self.client.post('/shizuha-pulse/api/custom-field-contexts/', {
            'custom_field': str(self.field.id),
            'name': 'Project Scoped',
            'visibility': 'required',
            'projects': [self.project.id],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        ctx = CustomFieldContext.objects.get(id=resp.json()['id'])
        self.assertEqual(ctx.projects.count(), 1)
        self.assertEqual(ctx.visibility, 'required')

    def test_create_issue_type_scoped_context(self):
        """Create a context scoped to specific issue types."""
        bug_type = IssueType.objects.get(slug='bug')
        resp = self.client.post('/shizuha-pulse/api/custom-field-contexts/', {
            'custom_field': str(self.field.id),
            'name': 'Bug Only',
            'visibility': 'visible',
            'issue_types': [str(bug_type.id)],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        ctx = CustomFieldContext.objects.get(id=resp.json()['id'])
        self.assertEqual(ctx.issue_types.count(), 1)

    def test_hidden_context(self):
        """Create a hidden context."""
        resp = self.client.post('/shizuha-pulse/api/custom-field-contexts/', {
            'custom_field': str(self.field.id),
            'name': 'Hidden Context',
            'visibility': 'hidden',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['visibility'], 'hidden')

    def test_context_with_options_override(self):
        """Context-specific options override for select fields."""
        select_field = CustomField.objects.create(
            name='Priority', slug='priority-ctx', field_type='select',
            options=[
                {'value': 'low', 'label': 'Low'},
                {'value': 'medium', 'label': 'Medium'},
                {'value': 'high', 'label': 'High'},
            ]
        )
        resp = self.client.post('/shizuha-pulse/api/custom-field-contexts/', {
            'custom_field': str(select_field.id),
            'name': 'Restricted Options',
            'options_override': [
                {'value': 'medium', 'label': 'Medium'},
                {'value': 'high', 'label': 'High'},
            ],
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(len(resp.json()['options_override']), 2)

    def test_list_contexts(self):
        resp = self.client.get('/shizuha-pulse/api/custom-field-contexts/')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

    def test_delete_context(self):
        ctx = CustomFieldContext.objects.create(
            custom_field=self.field, name='Delete Me'
        )
        resp = self.client.delete(f'/shizuha-pulse/api/custom-field-contexts/{ctx.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)


class CustomFieldValueAPITests(AuthenticatedAPITestCase):
    """Tests for CustomFieldValue API — storing values per item."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='Value Project', key='VAL', org_id=1
        )
        self.item = Item.objects.create(
            mode='task', title='Test Task', project=self.project,
            item_key='VAL-1', status='pending', created_by_id=self.user.id
        )
        self.text_field = CustomField.objects.create(
            name='Customer', slug='customer-val', field_type='text'
        )
        self.number_field = CustomField.objects.create(
            name='Points', slug='points-val', field_type='number'
        )
        self.select_field = CustomField.objects.create(
            name='Env', slug='env-val', field_type='select',
            options=[{'value': 'dev', 'label': 'Dev'}, {'value': 'prod', 'label': 'Prod'}]
        )

    def test_set_text_value(self):
        resp = self.client.post('/shizuha-pulse/api/custom-field-values/', {
            'item': self.item.id,
            'custom_field': str(self.text_field.id),
            'value': 'Acme Corp',
            'value_text': 'Acme Corp',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['value'], 'Acme Corp')

    def test_set_number_value(self):
        resp = self.client.post('/shizuha-pulse/api/custom-field-values/', {
            'item': self.item.id,
            'custom_field': str(self.number_field.id),
            'value': 13,
            'value_text': '13',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['value'], 13)

    def test_set_select_value(self):
        resp = self.client.post('/shizuha-pulse/api/custom-field-values/', {
            'item': self.item.id,
            'custom_field': str(self.select_field.id),
            'value': 'prod',
            'value_text': 'Production',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)

    def test_update_value(self):
        val = CustomFieldValue.objects.create(
            item=self.item, custom_field=self.text_field,
            value='Old', value_text='Old'
        )
        resp = self.client.patch(f'/shizuha-pulse/api/custom-field-values/{val.id}/', {
            'value': 'New Value',
            'value_text': 'New Value',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        val.refresh_from_db()
        self.assertEqual(val.value, 'New Value')

    def test_filter_by_item(self):
        CustomFieldValue.objects.create(
            item=self.item, custom_field=self.text_field,
            value='Test', value_text='Test'
        )
        resp = self.client.get(f'/shizuha-pulse/api/custom-field-values/?item_id={self.item.id}')
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        results = resp.json() if isinstance(resp.json(), list) else resp.json().get('results', resp.json())
        self.assertGreaterEqual(len(results), 1)

    def test_filter_by_custom_field(self):
        CustomFieldValue.objects.create(
            item=self.item, custom_field=self.text_field,
            value='Filter', value_text='Filter'
        )
        resp = self.client.get(
            f'/shizuha-pulse/api/custom-field-values/?custom_field_id={self.text_field.id}'
        )
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)

    def test_unique_item_field_constraint(self):
        """Same item + same custom field should not allow duplicate values."""
        CustomFieldValue.objects.create(
            item=self.item, custom_field=self.text_field,
            value='First', value_text='First'
        )
        resp = self.client.post('/shizuha-pulse/api/custom-field-values/', {
            'item': self.item.id,
            'custom_field': str(self.text_field.id),
            'value': 'Second',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_400_BAD_REQUEST)

    def test_delete_value(self):
        val = CustomFieldValue.objects.create(
            item=self.item, custom_field=self.text_field,
            value='Del', value_text='Del'
        )
        resp = self.client.delete(f'/shizuha-pulse/api/custom-field-values/{val.id}/')
        self.assertEqual(resp.status_code, http_status.HTTP_204_NO_CONTENT)

    def test_set_multiselect_value(self):
        """Multiselect stores array of values."""
        ms_field = CustomField.objects.create(
            name='Tags', slug='tags-val', field_type='multiselect',
            options=[
                {'value': 'a', 'label': 'A'},
                {'value': 'b', 'label': 'B'},
                {'value': 'c', 'label': 'C'},
            ]
        )
        resp = self.client.post('/shizuha-pulse/api/custom-field-values/', {
            'item': self.item.id,
            'custom_field': str(ms_field.id),
            'value': ['a', 'c'],
            'value_text': 'A, C',
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertEqual(resp.json()['value'], ['a', 'c'])

    def test_set_null_value(self):
        """Custom field values can be null (cleared)."""
        resp = self.client.post('/shizuha-pulse/api/custom-field-values/', {
            'item': self.item.id,
            'custom_field': str(self.text_field.id),
            'value': None,
        }, format='json')
        self.assertEqual(resp.status_code, http_status.HTTP_201_CREATED)
        self.assertIsNone(resp.json()['value'])
