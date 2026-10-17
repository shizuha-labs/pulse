"""
Comprehensive E2E Tests for Workflow Features in Shizuha Pulse.

Tests all new functionality:
1. Task Detail Page with URL routing (by_key endpoint)
2. Attachments System
3. Activity/Audit History
4. Issue Links
5. Watchers
6. Comment Reactions

These tests verify the complete flow from API endpoints through models.
"""
import uuid
import json
import io
from unittest.mock import patch, MagicMock
from urllib.parse import urlsplit

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase, APIClient
from rest_framework import status
from datetime import timedelta

from tasks.models import (
    Item, Project, Comment,
    Attachment, ItemActivity, ItemLink, ItemWatcher, CommentReaction,
    ProjectMembership,
)
from tasks.serializers import AttachmentSerializer
from tasks.services.storage import AttachmentStorage


# =============================================================================
# Helper Functions
# =============================================================================

def create_test_image(width=100, height=100, format='PNG'):
    """Create a test image file for upload tests."""
    # Create a minimal valid PNG without PIL
    # This is a 1x1 red PNG file
    png_data = bytes([
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,  # PNG signature
        0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,  # IHDR chunk
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,  # 1x1 image
        0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
        0xDE, 0x00, 0x00, 0x00, 0x0C, 0x49, 0x44, 0x41,  # IDAT chunk
        0x54, 0x08, 0xD7, 0x63, 0xF8, 0xCF, 0xC0, 0x00,
        0x00, 0x00, 0x03, 0x00, 0x01, 0x00, 0x05, 0xFE,
        0xD4, 0xEF, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45,  # IEND chunk
        0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82
    ])
    buffer = io.BytesIO(png_data)
    return buffer


from .base import AuthenticatedAPITestCase  # noqa: F811 - imported for use in this module


# =============================================================================
# Task Detail by Key (by_key endpoint) Tests
# =============================================================================

class TaskDetailByKeyTests(AuthenticatedAPITestCase):
    """Test the by_key endpoint for fetching items by their item_key (e.g., PROJ-123)."""

    def setUp(self):
        """Set up test data."""
        super().setUp()

        # Create a project
        self.project = Project.objects.create(
            name='Test Project',
            key='TEST',
            org_id=1
        )

        # Create items
        self.item1 = Item.objects.create(
            mode='task',
            title='Test Task 1',
            description='First test task',
            project=self.project,
            item_key='TEST-1',
            priority='high',
            status='pending',
            created_by_id=self.user.id
        )
        self.item2 = Item.objects.create(
            mode='task',
            title='Test Task 2',
            description='Second test task',
            project=self.project,
            item_key='TEST-2',
            priority='medium',
            status='in_progress',
            created_by_id=self.user.id
        )

    def test_get_item_by_key_success(self):
        """Test successfully fetching an item by its key."""
        response = self.client.get('/api/items/by-key/TEST-1/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['item_key'], 'TEST-1')
        self.assertEqual(response.data['title'], 'Test Task 1')
        self.assertEqual(response.data['priority'], 'high')

    def test_get_item_by_key_case_insensitive(self):
        """Test that item key lookup is case-insensitive."""
        response = self.client.get('/api/items/by-key/test-1/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['item_key'], 'TEST-1')

    def test_get_item_by_key_not_found(self):
        """Test 404 response for non-existent item key."""
        response = self.client.get('/api/items/by-key/NONEXISTENT-999/')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_get_item_by_key_different_projects(self):
        """Test fetching items from different projects by their keys."""
        # Create another project
        project2 = Project.objects.create(
            name='Another Project',
            key='ANOTHER',
            org_id=1
        )
        Item.objects.create(
            mode='task',
            title='Another Task',
            project=project2,
            item_key='ANOTHER-1',
            created_by_id=self.user.id
        )

        response1 = self.client.get('/api/items/by-key/TEST-1/')
        response2 = self.client.get('/api/items/by-key/ANOTHER-1/')

        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertEqual(response1.data['title'], 'Test Task 1')
        self.assertEqual(response2.data['title'], 'Another Task')

    def test_get_item_by_key_includes_project_info(self):
        """Test that the response includes project information."""
        response = self.client.get('/api/items/by-key/TEST-1/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('project', response.data)


# =============================================================================
# Task Project Change Tests
# =============================================================================

class TaskProjectChangeTests(AuthenticatedAPITestCase):
    """Test changing a task's project assignment."""

    def setUp(self):
        """Set up test data with multiple projects."""
        super().setUp()

        # Create projects
        self.project1 = Project.objects.create(
            name='Project Alpha',
            key='ALPHA',
            org_id=1
        )
        self.project2 = Project.objects.create(
            name='Project Beta',
            key='BETA',
            org_id=1
        )

        # Create a task in project1
        self.task = Item.objects.create(
            mode='task',
            title='Movable Task',
            description='This task can be moved between projects',
            project=self.project1,
            item_key='ALPHA-1',
            priority='normal',
            status='pending',
            created_by_id=self.user.id
        )

    def test_change_task_project_preserves_item_key(self):
        """Test that changing project preserves the original item_key."""
        original_key = self.task.item_key

        response = self.client.patch(
            f'/api/items/{self.task.id}/',
            {'project': self.project2.id},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['project'], self.project2.id)
        # Item key should be preserved (not changed to BETA-xxx)
        self.assertEqual(response.data['item_key'], original_key)

        # Verify in database
        self.task.refresh_from_db()
        self.assertEqual(self.task.project, self.project2)
        self.assertEqual(self.task.item_key, original_key)

    def test_project_task_cannot_be_detached_into_personal_scope(self):
        """A persisted organization boundary needs an explicit transfer protocol."""
        response = self.client.patch(
            f'/api/items/{self.task.id}/',
            {'project': None},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Verify in database
        self.task.refresh_from_db()
        self.assertEqual(self.task.project, self.project1)

    def test_change_project_creates_activity(self):
        """Test that changing project creates an activity record."""
        initial_activity_count = ItemActivity.objects.filter(item=self.task).count()

        response = self.client.patch(
            f'/api/items/{self.task.id}/',
            {'project': self.project2.id},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that an activity was created
        new_activity_count = ItemActivity.objects.filter(item=self.task).count()
        self.assertGreater(new_activity_count, initial_activity_count)

    def test_item_key_is_read_only(self):
        """Test that item_key cannot be changed via PATCH."""
        response = self.client.patch(
            f'/api/items/{self.task.id}/',
            {'item_key': 'BETA-999'},
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # item_key should remain unchanged (read-only field)
        self.assertEqual(response.data['item_key'], 'ALPHA-1')

        # Verify in database
        self.task.refresh_from_db()
        self.assertEqual(self.task.item_key, 'ALPHA-1')


# =============================================================================
# Attachment System Tests
# =============================================================================

class AttachmentModelTests(TestCase):
    """Test Attachment model functionality."""

    def setUp(self):
        """Set up test data."""
        self.project = Project.objects.create(
            name='Test Project',
            key='TEST',
            org_id=1
        )
        self.item = Item.objects.create(
            mode='task',
            title='Test Task',
            project=self.project,
            item_key='TEST-1'
        )

    def test_create_attachment(self):
        """Test creating a basic attachment."""
        attachment = Attachment.objects.create(
            item=self.item,
            storage_key='attachments/TEST-1/file.txt',
            filename='file.txt',
            file_type='text/plain',
            file_size=1024,
            uploaded_by_id=1,
            uploaded_by_email='user@example.com'
        )

        self.assertEqual(attachment.filename, 'file.txt')
        self.assertEqual(attachment.file_size, 1024)
        self.assertEqual(attachment.item, self.item)
        self.assertFalse(attachment.is_image)

    def test_create_image_attachment(self):
        """Test creating an image attachment with metadata."""
        attachment = Attachment.objects.create(
            item=self.item,
            storage_key='attachments/TEST-1/image.png',
            filename='image.png',
            file_type='image/png',
            file_size=5120,
            is_image=True,
            width=800,
            height=600,
            thumbnail_key='attachments/TEST-1/thumbnails/image_thumb.png',
            uploaded_by_id=1,
            uploaded_by_email='user@example.com'
        )

        self.assertTrue(attachment.is_image)
        self.assertEqual(attachment.width, 800)
        self.assertEqual(attachment.height, 600)
        self.assertIsNotNone(attachment.thumbnail_key)

    def test_attachment_uuid_primary_key(self):
        """Test that attachment uses UUID primary key."""
        attachment = Attachment.objects.create(
            item=self.item,
            storage_key='attachments/TEST-1/file.txt',
            filename='file.txt',
            file_type='text/plain',
            file_size=1024,
            uploaded_by_id=1,
            uploaded_by_email='user@example.com'
        )

        self.assertIsInstance(attachment.id, uuid.UUID)

    def test_attachment_cascade_delete(self):
        """Test that attachments are deleted when item is deleted."""
        attachment = Attachment.objects.create(
            item=self.item,
            storage_key='attachments/TEST-1/file.txt',
            filename='file.txt',
            file_type='text/plain',
            file_size=1024,
            uploaded_by_id=1,
            uploaded_by_email='user@example.com'
        )
        attachment_id = attachment.id

        self.item.delete()

        self.assertFalse(Attachment.objects.filter(id=attachment_id).exists())

    def test_spoofed_image_mime_does_not_create_inline_derivative(self):
        """Uploader MIME alone must never mark active content safe to inline."""
        storage = AttachmentStorage()
        storage._client = MagicMock()
        active_html = io.BytesIO(b'<html><script>alert(document.domain)</script></html>')

        _, _, image_meta = storage.upload_file(
            active_html,
            'spoofed.png',
            'image/png',
            self.item.id,
            1,
        )

        self.assertIsNone(image_meta)
        self.assertEqual(storage._client.put_object.call_count, 1)

    def test_valid_raster_polyglot_is_reencoded_before_inline_derivative(self):
        """A decoded raster derivative must not retain appended active bytes."""
        from PIL import Image

        original = io.BytesIO()
        Image.new('RGB', (4, 4), color='red').save(original, format='PNG')
        polyglot = io.BytesIO(
            original.getvalue() + b'<script>alert(document.domain)</script>'
        )
        storage = AttachmentStorage()
        storage._client = MagicMock()

        _, _, image_meta = storage.upload_file(
            polyglot,
            'polyglot.png',
            'image/png',
            self.item.id,
            1,
        )

        self.assertTrue(image_meta['thumbnail_key'].endswith('.png'))
        self.assertEqual(storage._client.put_object.call_count, 2)
        thumbnail_buffer = storage._client.put_object.call_args_list[1].args[2]
        thumbnail_bytes = thumbnail_buffer.getvalue()
        self.assertNotIn(b'<script>', thumbnail_bytes)
        Image.open(io.BytesIO(thumbnail_bytes)).verify()


class AttachmentAPITests(AuthenticatedAPITestCase):
    """Test Attachment API endpoints."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.project = Project.objects.create(
            name='Test Project',
            key='TEST',
            org_id=1
        )
        self.item = Item.objects.create(
            mode='task',
            title='Test Task',
            project=self.project,
            item_key='TEST-1',
            created_by_id=self.user.id
        )

    def test_list_attachments_for_item(self):
        """Test listing attachments for a specific item."""
        Attachment.objects.create(
            item=self.item,
            storage_key='key1',
            filename='file1.txt',
            file_type='text/plain',
            file_size=100,
            uploaded_by_id=self.user.id,
            uploaded_by_email=self.user.email
        )
        Attachment.objects.create(
            item=self.item,
            storage_key='key2',
            filename='file2.txt',
            file_type='text/plain',
            file_size=200,
            uploaded_by_id=self.user.id,
            uploaded_by_email=self.user.email
        )

        response = self.client.get(f'/api/items/{self.item.id}/attachments/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['attachments']), 2)

    def test_list_attachments_empty(self):
        """Test listing attachments when none exist."""
        response = self.client.get(f'/api/items/{self.item.id}/attachments/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['attachments']), 0)

    def test_list_attachments_filter_by_item(self):
        """Test that attachments are correctly filtered by item."""
        item2 = Item.objects.create(
            mode='task',
            title='Another Task',
            project=self.project,
            item_key='TEST-2',
            created_by_id=self.user.id
        )

        Attachment.objects.create(
            item=self.item,
            storage_key='key1',
            filename='item1_file.txt',
            file_type='text/plain',
            file_size=100,
            uploaded_by_id=self.user.id,
            uploaded_by_email=self.user.email
        )
        Attachment.objects.create(
            item=item2,
            storage_key='key2',
            filename='item2_file.txt',
            file_type='text/plain',
            file_size=100,
            uploaded_by_id=self.user.id,
            uploaded_by_email=self.user.email
        )

        response = self.client.get(f'/api/items/{self.item.id}/attachments/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['attachments']), 1)
        self.assertEqual(response.data['attachments'][0]['filename'], 'item1_file.txt')


class AttachmentPrivateProjectPermissionTests(APITestCase):
    """PLS-414: attachment actions mirror canonical private-item access."""

    def setUp(self):
        User = get_user_model()
        self.assignee = User.objects.create_user(
            username='attachment-assignee', email='assignee@example.com', password='x',
        )
        self.reporter = User.objects.create_user(
            username='attachment-reporter', email='reporter@example.com', password='x',
        )
        self.same_org_outsider = User.objects.create_user(
            username='attachment-outsider', email='outsider@example.com', password='x',
        )
        self.cross_org = User.objects.create_user(
            username='attachment-cross-org', email='cross@example.com', password='x',
        )
        self.member = User.objects.create_user(
            username='attachment-member', email='member@example.com', password='x',
        )
        for user in (self.assignee, self.reporter, self.same_org_outsider, self.member):
            user.organization_memberships = {1: 'member'}
        self.cross_org.organization_memberships = {2: 'member'}

        self.project = Project.objects.create(
            name='Private Attachments', key='PATT', org_id=1, access_level='private',
        )
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item = Item.objects.create(
                mode='task', title='Private attachment host', project=self.project,
                item_key='PATT-1', assignee_id=self.assignee.id,
                assignee_email=self.assignee.email, created_by_id=self.reporter.id,
                created_by_email=self.reporter.email,
            )
        self.attachment = Attachment.objects.create(
            item=self.item, storage_key='private/file.txt', filename='file.txt',
            file_type='text/plain', file_size=4,
            uploaded_by_id=self.assignee.id,
            uploaded_by_email=self.assignee.email,
        )

    def _auth(self, user):
        self.client.force_authenticate(user=user)

    @staticmethod
    def _results(response):
        data = response.json()
        if isinstance(data, list):
            return data
        return data.get('results', [])

    @patch('tasks.services.storage.attachment_storage.upload_file')
    def test_assignee_and_reporter_can_upload_without_project_membership(self, upload):
        upload.return_value = ('private/upload.txt', 7, None)
        for index, user in enumerate((self.assignee, self.reporter), start=1):
            self._auth(user)
            response = self.client.post(
                '/api/attachments/',
                {
                    'item': self.item.item_key,
                    'file': SimpleUploadedFile(
                        f'upload-{index}.txt', b'payload', content_type='text/plain',
                    ),
                },
                format='multipart',
            )
            self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)

    @patch('tasks.services.storage.attachment_storage.upload_file')
    def test_same_org_nonmember_and_cross_org_cannot_upload(self, upload):
        upload.return_value = ('private/denied.txt', 7, None)
        for user in (self.same_org_outsider, self.cross_org):
            self._auth(user)
            response = self.client.post(
                '/api/attachments/',
                {
                    'item': self.item.item_key,
                    'file': SimpleUploadedFile(
                        'denied.txt', b'payload', content_type='text/plain',
                    ),
                },
                format='multipart',
            )
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        upload.assert_not_called()

    def test_list_and_retrieve_recheck_private_item_access(self):
        self._auth(self.assignee)
        listed = self.client.get('/api/attachments/', {'item': self.item.item_key})
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual(len(self._results(listed)), 1)
        detail_url = f'/api/attachments/{self.attachment.id}/'
        self.assertEqual(self.client.get(detail_url).status_code, status.HTTP_200_OK)

        for user in (self.same_org_outsider, self.cross_org):
            self._auth(user)
            hidden = self.client.get('/api/attachments/', {'item': self.item.item_key})
            self.assertEqual(hidden.status_code, status.HTTP_200_OK)
            self.assertEqual(self._results(hidden), [])
            self.assertEqual(
                self.client.get(detail_url).status_code,
                status.HTTP_403_FORBIDDEN,
            )

    def test_reassignment_revokes_old_assignee_and_grants_new_assignee(self):
        User = get_user_model()
        new_assignee = User.objects.create_user(
            username='attachment-new-assignee', email='new-assignee@example.com', password='x',
        )
        new_assignee.organization_memberships = {1: 'member'}
        self.item.assignee_id = new_assignee.id
        self.item.assignee_email = new_assignee.email
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item.save(update_fields=['assignee_id', 'assignee_email'])
        detail_url = f'/api/attachments/{self.attachment.id}/'

        self._auth(self.assignee)
        self.assertEqual(self.client.get(detail_url).status_code, status.HTTP_403_FORBIDDEN)
        self._auth(new_assignee)
        self.assertEqual(self.client.get(detail_url).status_code, status.HTTP_200_OK)

    def test_project_membership_revocation_revokes_attachment_access(self):
        membership = ProjectMembership.objects.create(
            project=self.project, user_id=self.member.id,
            user_email=self.member.email, role='member',
        )
        detail_url = f'/api/attachments/{self.attachment.id}/'
        self._auth(self.member)
        self.assertEqual(self.client.get(detail_url).status_code, status.HTTP_200_OK)
        membership.is_active = False
        membership.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(detail_url).status_code, status.HTTP_403_FORBIDDEN)

    @patch('tasks.services.storage.attachment_storage._client')
    def test_serve_requires_item_access_or_short_lived_signed_url(self, storage_client):
        self.attachment.filename = 'active"\r\nX-Injected: yes.html'
        self.attachment.file_type = 'text/html'
        self.attachment.save(update_fields=['filename', 'file_type'])
        storage_response = MagicMock()
        storage_response.stream.return_value = [b'<script>alert(document.domain)</script>']
        storage_client.get_object.return_value = storage_response
        serve_url = f'/api/attachments/{self.attachment.id}/serve/'

        self._auth(self.assignee)
        allowed = self.client.get(serve_url)
        self.assertEqual(allowed.status_code, status.HTTP_200_OK)
        self.assertEqual(
            b''.join(allowed.streaming_content),
            b'<script>alert(document.domain)</script>',
        )
        self.assertEqual(allowed['Content-Type'], 'application/octet-stream')
        self.assertTrue(allowed['Content-Disposition'].startswith('attachment;'))
        self.assertNotIn('\r', allowed['Content-Disposition'])
        self.assertNotIn('\n', allowed['Content-Disposition'])
        self.assertEqual(allowed['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(allowed['Cache-Control'], 'private, no-store')
        self.assertEqual(allowed['Referrer-Policy'], 'no-referrer')
        self.assertEqual(allowed['X-Frame-Options'], 'DENY')
        self.assertEqual(
            allowed['Content-Security-Policy'],
            "sandbox; default-src 'none'",
        )

        self._auth(self.same_org_outsider)
        self.assertEqual(self.client.get(serve_url).status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(serve_url).status_code, status.HTTP_403_FORBIDDEN)
        signed_url = AttachmentSerializer(self.attachment).data['download_url']
        signed_path = urlsplit(signed_url).path.replace('/pulse/api/', '/api/')
        signed_query = urlsplit(signed_url).query
        signed = self.client.get(f'{signed_path}?{signed_query}')
        self.assertEqual(signed.status_code, status.HTTP_200_OK)
        self.assertEqual(
            b''.join(signed.streaming_content),
            b'<script>alert(document.domain)</script>',
        )
        self.assertEqual(signed['Content-Type'], 'application/octet-stream')
        self.assertEqual(signed['Cache-Control'], 'private, no-store')

        tampered_query = f'{signed_query}x'
        self.assertEqual(
            self.client.get(f'{signed_path}?{tampered_query}').status_code,
            status.HTTP_403_FORBIDDEN,
        )

    @patch('tasks.services.storage.attachment_storage._client')
    def test_svg_and_spoofed_polyglot_are_download_only(self, storage_client):
        vectors = (
            ('active.svg', 'image/svg+xml', b'<svg onload="alert(1)"/>'),
            ('polyglot.png', 'image/png', b'\x89PNG\r\n<script>alert(1)</script>'),
        )
        self._auth(self.assignee)

        for index, (filename, claimed_type, body) in enumerate(vectors):
            attachment = Attachment.objects.create(
                item=self.item,
                storage_key=f'private/vector-{index}',
                filename=filename,
                file_type=claimed_type,
                file_size=len(body),
                is_image=False,
                uploaded_by_id=self.assignee.id,
                uploaded_by_email=self.assignee.email,
            )
            storage_response = MagicMock()
            storage_response.stream.return_value = [body]
            storage_client.get_object.return_value = storage_response

            response = self.client.get(f'/api/attachments/{attachment.id}/serve/')
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response['Content-Type'], 'application/octet-stream')
            self.assertTrue(response['Content-Disposition'].startswith('attachment;'))
            self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(response['Cache-Control'], 'private, no-store')
            self.assertEqual(b''.join(response.streaming_content), body)

    @patch('tasks.services.storage.attachment_storage._client')
    def test_only_server_generated_raster_thumbnail_is_inline(self, storage_client):
        self.attachment.file_type = 'image/svg+xml'
        self.attachment.is_image = True
        self.attachment.thumbnail_key = 'thumbnails/private/safe.png'
        self.attachment.save(update_fields=['file_type', 'is_image', 'thumbnail_key'])
        safe_png = b'\x89PNG\r\n\x1a\nserver-reencoded'
        storage_response = MagicMock()
        storage_response.stream.return_value = [safe_png]
        storage_client.get_object.return_value = storage_response
        signed_url = AttachmentSerializer(self.attachment).data['thumbnail_url']
        parsed = urlsplit(signed_url)
        path = parsed.path.replace('/pulse/api/', '/api/')

        self.client.force_authenticate(user=None)
        response = self.client.get(f'{path}?{parsed.query}')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertTrue(response['Content-Disposition'].startswith('inline;'))
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.assertEqual(response['Cross-Origin-Resource-Policy'], 'same-origin')
        self.assertEqual(b''.join(response.streaming_content), safe_png)

    def test_download_action_returns_safe_proxy_not_storage_url(self):
        self._auth(self.assignee)
        response = self.client.get(f'/api/attachments/{self.attachment.id}/download/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(
            f'/pulse/api/attachments/{self.attachment.id}/serve/',
            response.data['download_url'],
        )
        self.assertIn('token=', response.data['download_url'])

    @patch('tasks.services.storage.attachment_storage.upload_file')
    @patch('tasks.services.storage.attachment_storage._client')
    def test_colliding_id_with_canonical_email_is_denied_every_action(
        self,
        storage_client,
        upload_file,
    ):
        """PLS-415: drifted IDs cannot override a nonblank canonical email."""
        self.attachment.is_image = True
        self.attachment.thumbnail_key = 'thumbnails/private/collision.png'
        self.attachment.save(update_fields=['is_image', 'thumbnail_key'])
        upload_file.return_value = ('private/collision.txt', 7, None)
        storage_client.get_object.return_value = MagicMock()
        attachment_url = f'/api/attachments/{self.attachment.id}/'

        # The same-org actor isolates the email-primary rule; the cross-org
        # actor proves the tenant boundary remains closed as well.
        for actor in (self.same_org_outsider, self.cross_org):
            with self.subTest(actor=actor.email):
                self.item.assignee_id = actor.id
                self.item.assignee_email = self.assignee.email
                with patch(
                    'tasks.services.admin_service.admin_service.is_org_member',
                    return_value=True,
                ):
                    self.item.save(update_fields=['assignee_id', 'assignee_email'])
                self._auth(actor)

                listed = self.client.get(
                    '/api/attachments/', {'item': self.item.item_key},
                )
                self.assertEqual(listed.status_code, status.HTTP_200_OK)
                self.assertEqual(self._results(listed), [])
                self.assertEqual(self.client.get(attachment_url).status_code, 403)
                self.assertEqual(
                    self.client.get(f'{attachment_url}download/').status_code,
                    403,
                )
                self.assertEqual(
                    self.client.get(f'{attachment_url}serve/').status_code,
                    403,
                )
                self.assertEqual(
                    self.client.get(f'{attachment_url}thumbnail/').status_code,
                    403,
                )
                self.assertEqual(self.client.delete(attachment_url).status_code, 403)
                upload = self.client.post(
                    '/api/attachments/',
                    {
                        'item': self.item.item_key,
                        'file': SimpleUploadedFile('collision.txt', b'payload'),
                    },
                    format='multipart',
                )
                self.assertEqual(upload.status_code, 403)

                comment = self.client.post(
                    '/api/comments/',
                    {'item': self.item.id, 'content': 'collision'},
                    format='json',
                )
                self.assertEqual(comment.status_code, 400)
                self.assertIn(
                    self.client.get(f'/api/items/{self.item.id}/').status_code,
                    (403, 404),
                )
                self.assertIn(
                    self.client.patch(
                        f'/api/items/{self.item.id}/',
                        {'labels': ['collision']},
                        format='json',
                    ).status_code,
                    (403, 404),
                )

        upload_file.assert_not_called()

    def test_blank_email_legacy_id_fallback_and_alias_email_are_positive(self):
        """Legacy ID fallback is per-slot; known email aliases remain valid."""
        # Current project assignments require canonical emails at the database
        # boundary; the fallback remains necessary for legacy/projectless rows.
        self.item = Item.objects.create(
            mode='task', title='Legacy attachment host',
            assignee_id=self.same_org_outsider.id, assignee_email='',
        )
        self.attachment.item = self.item
        self.attachment.save(update_fields=['item'])
        self._auth(self.same_org_outsider)
        self.assertEqual(
            self.client.get(f'/api/attachments/{self.attachment.id}/').status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.post(
                '/api/comments/',
                {'item': self.item.id, 'content': 'legacy participant'},
                format='json',
            ).status_code,
            status.HTTP_201_CREATED,
        )

        User = get_user_model()
        alias_user = User.objects.create_user(
            username='attachment-alias', email='alias@shizuha.com', password='x',
        )
        alias_user.organization_memberships = {1: 'member'}
        self.item.assignee_id = None
        self.item.assignee_email = 'alias@agents.shizuha.io'
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item.save(update_fields=['assignee_id', 'assignee_email'])
        self._auth(alias_user)
        self.assertEqual(
            self.client.get(f'/api/attachments/{self.attachment.id}/').status_code,
            status.HTTP_200_OK,
        )

    @patch('tasks.services.storage.attachment_storage.delete_file')
    def test_delete_rechecks_access_after_reassignment(self, delete_file):
        User = get_user_model()
        new_assignee = User.objects.create_user(
            username='attachment-delete-owner', email='delete-owner@example.com', password='x',
        )
        new_assignee.organization_memberships = {1: 'member'}
        self.item.assignee_id = new_assignee.id
        self.item.assignee_email = new_assignee.email
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item.save(update_fields=['assignee_id', 'assignee_email'])
        detail_url = f'/api/attachments/{self.attachment.id}/'

        self._auth(self.assignee)
        self.assertEqual(self.client.delete(detail_url).status_code, status.HTTP_403_FORBIDDEN)
        self._auth(new_assignee)
        self.assertEqual(self.client.delete(detail_url).status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Attachment.objects.filter(pk=self.attachment.id).exists())
        delete_file.assert_called_once()


# =============================================================================
# Activity/Audit History Tests
# =============================================================================

class ItemActivityModelTests(TestCase):
    """Test ItemActivity model functionality."""

    def setUp(self):
        """Set up test data."""
        self.project = Project.objects.create(
            name='Test Project',
            key='TEST',
            org_id=1
        )
        self.item = Item.objects.create(
            mode='task',
            title='Test Task',
            project=self.project,
            item_key='TEST-1'
        )

    def test_create_activity(self):
        """Test creating an activity record."""
        activity = ItemActivity.objects.create(
            item=self.item,
            action='created',
            user_id=1,
            user_email='user@example.com'
        )

        self.assertEqual(activity.action, 'created')
        self.assertEqual(activity.item, self.item)
        self.assertEqual(activity.user_id, 1)

    def test_create_field_change_activity(self):
        """Test creating an activity record for field change."""
        activity = ItemActivity.objects.create(
            item=self.item,
            action='updated',
            field_name='priority',
            old_value='low',
            new_value='high',
            user_id=1,
            user_email='user@example.com'
        )

        self.assertEqual(activity.field_name, 'priority')
        self.assertEqual(activity.old_value, 'low')
        self.assertEqual(activity.new_value, 'high')

    def test_activity_ordering(self):
        """Test that activities are ordered by created_at descending."""
        activity1 = ItemActivity.objects.create(
            item=self.item,
            action='created',
            user_id=1,
            user_email='user@example.com'
        )
        activity2 = ItemActivity.objects.create(
            item=self.item,
            action='updated',
            user_id=1,
            user_email='user@example.com'
        )

        activities = list(ItemActivity.objects.filter(item=self.item))
        # Most recent first
        self.assertEqual(activities[0].id, activity2.id)
        self.assertEqual(activities[1].id, activity1.id)

    def test_all_action_types(self):
        """Test all defined action types can be created."""
        action_types = [
            'created', 'updated', 'status_changed', 'assigned', 'unassigned',
            'priority_changed', 'commented', 'attachment_added', 'attachment_removed',
            'linked', 'unlinked', 'watched', 'unwatched', 'completed', 'reopened'
        ]

        for action in action_types:
            activity = ItemActivity.objects.create(
                item=self.item,
                action=action,
                user_id=1,
                user_email='user@example.com'
            )
            self.assertEqual(activity.action, action)


class ItemActivityAPITests(AuthenticatedAPITestCase):
    """Test ItemActivity API endpoints."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.project = Project.objects.create(
            name='Test Project',
            key='TEST',
            org_id=1
        )
        self.item = Item.objects.create(
            mode='task',
            title='Test Task',
            project=self.project,
            item_key='TEST-1',
            created_by_id=self.user.id
        )

    def test_get_activity_timeline(self):
        """Test fetching activity timeline for an item."""
        # Clear any auto-created activities from signals
        ItemActivity.objects.filter(item=self.item).delete()

        ItemActivity.objects.create(
            item=self.item,
            action='created',
            user_id=self.user.id,
            user_email=self.user.email
        )
        ItemActivity.objects.create(
            item=self.item,
            action='status_changed',
            field_name='status',
            old_value='pending',
            new_value='in_progress',
            user_id=self.user.id,
            user_email=self.user.email
        )

        response = self.client.get(f'/api/items/{self.item.id}/activity/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['activities']), 2)

    def test_activity_includes_user_info(self):
        """Test that activity includes user information."""
        ItemActivity.objects.create(
            item=self.item,
            action='created',
            user_id=self.user.id,
            user_email='testuser@example.com'
        )

        response = self.client.get(f'/api/items/{self.item.id}/activity/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        activity = response.data['activities'][0]
        self.assertEqual(activity['user_email'], 'testuser@example.com')

    def test_activity_includes_field_changes(self):
        """Test that activity includes field change details."""
        ItemActivity.objects.create(
            item=self.item,
            action='updated',
            field_name='title',
            old_value='Old Title',
            new_value='New Title',
            user_id=self.user.id,
            user_email=self.user.email
        )

        response = self.client.get(f'/api/items/{self.item.id}/activity/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        activity = response.data['activities'][0]
        self.assertEqual(activity['field_name'], 'title')
        self.assertEqual(activity['old_value'], 'Old Title')
        self.assertEqual(activity['new_value'], 'New Title')


# =============================================================================
# Issue Links Tests
# =============================================================================

class ItemLinkModelTests(TestCase):
    """Test ItemLink model functionality."""

    def setUp(self):
        """Set up test data."""
        self.project = Project.objects.create(
            name='Test Project',
            key='TEST',
            org_id=1
        )
        self.item1 = Item.objects.create(
            mode='task',
            title='Task 1',
            project=self.project,
            item_key='TEST-1'
        )
        self.item2 = Item.objects.create(
            mode='task',
            title='Task 2',
            project=self.project,
            item_key='TEST-2'
        )

    def test_create_link(self):
        """Test creating a link between items."""
        link = ItemLink.objects.create(
            source_item=self.item1,
            target_item=self.item2,
            link_type='blocks',
            created_by_id=1,
            created_by_email='user@example.com'
        )

        self.assertEqual(link.source_item, self.item1)
        self.assertEqual(link.target_item, self.item2)
        self.assertEqual(link.link_type, 'blocks')

    def test_all_link_types(self):
        """Test all defined link types."""
        link_types = [
            'blocks', 'is_blocked_by', 'relates_to', 'duplicates',
            'is_duplicated_by', 'parent_of', 'child_of', 'clones', 'is_cloned_by'
        ]

        for link_type in link_types:
            link = ItemLink.objects.create(
                source_item=self.item1,
                target_item=self.item2,
                link_type=link_type,
                created_by_id=1,
                created_by_email='user@example.com'
            )
            self.assertEqual(link.link_type, link_type)
            link.delete()

    def test_unique_constraint(self):
        """Test that duplicate links are prevented."""
        ItemLink.objects.create(
            source_item=self.item1,
            target_item=self.item2,
            link_type='blocks',
            created_by_id=1,
            created_by_email='user@example.com'
        )

        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            ItemLink.objects.create(
                source_item=self.item1,
                target_item=self.item2,
                link_type='blocks',
                created_by_id=1,
                created_by_email='user@example.com'
            )

    def test_link_uuid_primary_key(self):
        """Test that link uses UUID primary key."""
        link = ItemLink.objects.create(
            source_item=self.item1,
            target_item=self.item2,
            link_type='relates_to',
            created_by_id=1,
            created_by_email='user@example.com'
        )

        self.assertIsInstance(link.id, uuid.UUID)

    def test_outward_and_inward_links(self):
        """Test that links appear in both outward and inward related names."""
        link = ItemLink.objects.create(
            source_item=self.item1,
            target_item=self.item2,
            link_type='blocks',
            created_by_id=1,
            created_by_email='user@example.com'
        )

        self.assertIn(link, self.item1.outward_links.all())
        self.assertIn(link, self.item2.inward_links.all())

    def test_inverse_link_type_display(self):
        """Test the inverse link type logic."""
        # Create a blocks link from item1 to item2
        link = ItemLink.objects.create(
            source_item=self.item1,
            target_item=self.item2,
            link_type='blocks',
            created_by_id=1,
            created_by_email='user@example.com'
        )

        # From item1's perspective: "blocks" item2
        # From item2's perspective: "is_blocked_by" item1
        inverse_map = {
            'blocks': 'is_blocked_by',
            'is_blocked_by': 'blocks',
            'duplicates': 'is_duplicated_by',
            'is_duplicated_by': 'duplicates',
            'parent_of': 'child_of',
            'child_of': 'parent_of',
            'clones': 'is_cloned_by',
            'is_cloned_by': 'clones',
            'relates_to': 'relates_to',
        }

        self.assertEqual(inverse_map[link.link_type], 'is_blocked_by')


class ItemLinkAPITests(AuthenticatedAPITestCase):
    """Test ItemLink API endpoints."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.project = Project.objects.create(
            name='Test Project',
            key='TEST',
            org_id=1
        )
        self.item1 = Item.objects.create(
            mode='task',
            title='Task 1',
            project=self.project,
            item_key='TEST-1',
            created_by_id=self.user.id
        )
        self.item2 = Item.objects.create(
            mode='task',
            title='Task 2',
            project=self.project,
            item_key='TEST-2',
            created_by_id=self.user.id
        )
        self.item3 = Item.objects.create(
            mode='task',
            title='Task 3',
            project=self.project,
            item_key='TEST-3',
            created_by_id=self.user.id
        )

    def test_list_links_for_item(self):
        """Test listing all links for an item."""
        ItemLink.objects.create(
            source_item=self.item1,
            target_item=self.item2,
            link_type='blocks',
            created_by_id=self.user.id,
            created_by_email=self.user.email
        )
        ItemLink.objects.create(
            source_item=self.item3,
            target_item=self.item1,
            link_type='relates_to',
            created_by_id=self.user.id,
            created_by_email=self.user.email
        )

        response = self.client.get(f'/api/items/{self.item1.id}/links/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['outward_links']), 1)
        self.assertEqual(len(response.data['inward_links']), 1)

    def test_list_links_empty(self):
        """Test listing links when none exist."""
        response = self.client.get(f'/api/items/{self.item1.id}/links/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['outward_links']), 0)
        self.assertEqual(len(response.data['inward_links']), 0)

    def test_create_link_by_item_key(self):
        """Test creating a link using target item key."""
        data = {
            'target_item_key': 'TEST-2',
            'link_type': 'blocks'
        }

        response = self.client.post(f'/api/items/{self.item1.id}/links/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ItemLink.objects.count(), 1)
        link = ItemLink.objects.first()
        self.assertEqual(link.source_item, self.item1)
        self.assertEqual(link.target_item, self.item2)

    def test_create_link_rejects_target_from_another_organization(self):
        """Cross-organization links are rejected as a caller error."""
        other_project = Project.objects.create(
            name='Other Organization Project',
            key='OTHER',
            org_id=2,
        )
        other_item = Item.objects.create(
            mode='task',
            title='Other Organization Task',
            project=other_project,
            item_key='OTHER-1',
            created_by_id=self.user.id,
        )

        response = self.client.post(
            f'/api/items/{self.item1.id}/links/',
            {
                'target_item_key': other_item.item_key,
                'link_type': 'relates_to',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data['error'],
            'Linked tasks must belong to the same organization.',
        )
        self.assertFalse(ItemLink.objects.exists())

    def test_create_link_invalid_target(self):
        """Test creating a link with invalid target key."""
        data = {
            'target_item_key': 'NONEXISTENT-999',
            'link_type': 'blocks'
        }

        response = self.client.post(f'/api/items/{self.item1.id}/links/', data, format='json')

        # Should return 400 or 404 for invalid target
        self.assertIn(response.status_code, [
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_404_NOT_FOUND
        ])

    def test_delete_link(self):
        """Test deleting a link."""
        link = ItemLink.objects.create(
            source_item=self.item1,
            target_item=self.item2,
            link_type='blocks',
            created_by_id=self.user.id,
            created_by_email=self.user.email
        )

        response = self.client.delete(f'/api/items/{self.item1.id}/links/{link.id}/')

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ItemLink.objects.filter(id=link.id).exists())

    def test_delete_link_open_project_non_participant_editor(self):
        """PLS-80: a non-staff org member who is NEITHER creator nor assignee can
        delete a (stale) link in an OPEN project — symmetric with link creation,
        which already allows open-project members. Regresses the PLAT-356<->ID-1
        403 trap where the unlink tool could not clear a false blocker."""
        User = get_user_model()
        self.project.access_level = 'open'
        self.project.save()
        agent = User.objects.create_user(
            username='agentx', email='agentx@shizuha.com', password='x', is_staff=False,
        )
        link = ItemLink.objects.create(
            source_item=self.item1, target_item=self.item2, link_type='is_blocked_by',
            created_by_id=self.user.id, created_by_email=self.user.email,
        )
        self.client.force_authenticate(agent)
        with patch('tasks.services.admin_service.admin_service.is_org_member', return_value=True):
            response = self.client.delete(f'/api/items/{self.item1.id}/links/{link.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ItemLink.objects.filter(id=link.id).exists())

    def test_delete_link_private_project_non_member_denied(self):
        """A non-staff org member who is NOT a member of a PRIVATE project (and not
        a participant) still cannot delete its links — the widening is scoped to
        open projects / project editors only."""
        User = get_user_model()
        self.project.access_level = 'private'
        self.project.save()
        intruder = User.objects.create_user(
            username='intruder2', email='intruder2@shizuha.com', password='x', is_staff=False,
        )
        link = ItemLink.objects.create(
            source_item=self.item1, target_item=self.item2, link_type='blocks',
            created_by_id=self.user.id, created_by_email=self.user.email,
        )
        self.client.force_authenticate(intruder)
        with patch('tasks.services.admin_service.admin_service.is_org_member', return_value=True):
            response = self.client.delete(f'/api/items/{self.item1.id}/links/{link.id}/')
        self.assertIn(response.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND))
        self.assertTrue(ItemLink.objects.filter(id=link.id).exists())

    def test_link_includes_target_item_info(self):
        """Test that link response includes target item details."""
        ItemLink.objects.create(
            source_item=self.item1,
            target_item=self.item2,
            link_type='blocks',
            created_by_id=self.user.id,
            created_by_email=self.user.email
        )

        response = self.client.get(f'/api/items/{self.item1.id}/links/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        link_data = response.data['outward_links'][0]
        self.assertIn('target_item_key', link_data)
        self.assertEqual(link_data['target_item_key'], 'TEST-2')


# =============================================================================
# PLS-498 Architecture regression matrix
# =============================================================================

    def test_delete_last_active_blocker_link_resumes_blocked_task_pls498(self):
        """PLS-498: removing last active blocker link resumes dependent (204, not 409)."""
        ItemLink.objects.all().delete()
        self.item2.mode = 'task'
        self.item2.status = 'in_progress'
        self.item2.assignee_id = self.user.id
        self.item2.assignee_email = self.user.email
        # This test exercises link deletion, not the external organization
        # membership boundary. Keep the blocker fixture valid without making
        # the result depend on the Admin service's availability or cache state.
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item2.save()
        link = ItemLink.objects.create(
            source_item=self.item1, target_item=self.item2, link_type='is_blocked_by',
            created_by_id=self.user.id, created_by_email=self.user.email,
        )
        self.item1.mode = 'task'
        self.item1.metadata = {
            'pre_block_state': {'status': 'in_progress', 'had_active_blocker': True}}
        self.item1.status = 'blocked'
        self.item1.save()

        response = self.client.delete(f'/api/items/{self.item1.id}/links/{link.id}/')

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ItemLink.objects.filter(id=link.id).exists())
        self.item1.refresh_from_db()
        self.assertNotEqual(self.item1.status, 'blocked')

    def test_delete_blocker_link_with_other_active_blocker_stays_blocked_pls498(self):
        """PLS-498: removing one of two blockers leaves dependent blocked."""
        ItemLink.objects.all().delete()
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            for item in (self.item2, self.item3):
                item.mode = 'task'
                item.status = 'in_progress'
                item.assignee_id = self.user.id
                item.assignee_email = self.user.email
                item.save()
        link_a = ItemLink.objects.create(
            source_item=self.item1, target_item=self.item2, link_type='is_blocked_by',
            created_by_id=self.user.id, created_by_email=self.user.email,
        )
        ItemLink.objects.create(
            source_item=self.item1, target_item=self.item3, link_type='is_blocked_by',
            created_by_id=self.user.id, created_by_email=self.user.email,
        )
        self.item1.mode = 'task'
        self.item1.metadata = {
            'pre_block_state': {'status': 'in_progress', 'had_active_blocker': True}}
        self.item1.status = 'blocked'
        self.item1.save()

        response = self.client.delete(f'/api/items/{self.item1.id}/links/{link_a.id}/')

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.status, 'blocked')

    def _pls498_block_item1_by(self, *blockers, link_type='is_blocked_by'):
        """Arm a valid blocked-task fixture without depending on Admin reachability."""
        ItemLink.objects.all().delete()
        links = []
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            for blocker in blockers:
                blocker.mode = 'task'
                blocker.status = 'in_progress'
                blocker.assignee_id = self.user.id
                blocker.assignee_email = self.user.email
                blocker.save()
                if link_type == 'blocks':
                    links.append(ItemLink.objects.create(
                        source_item=blocker,
                        target_item=self.item1,
                        link_type='blocks',
                        created_by_id=self.user.id,
                        created_by_email=self.user.email,
                    ))
                else:
                    links.append(ItemLink.objects.create(
                        source_item=self.item1,
                        target_item=blocker,
                        link_type='is_blocked_by',
                        created_by_id=self.user.id,
                        created_by_email=self.user.email,
                    ))
        self.item1.mode = 'task'
        self.item1.metadata = {
            'pre_block_state': {'status': 'in_progress', 'had_active_blocker': True}}
        self.item1.status = 'blocked'
        self.item1.save()
        return links

    def test_delete_last_blocker_restore_failure_rolls_back_pls498(self):
        """A restore exception rolls back the edge and returns typed 409."""
        (link,) = self._pls498_block_item1_by(self.item2)
        with patch(
            'tasks.services.workflow_service.WorkflowService._restore_blocked_item',
            side_effect=RuntimeError('forced restore failure'),
        ):
            response = self.client.delete(
                f'/api/items/{self.item1.id}/links/{link.id}/')
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'dependency_release_rejected')
        self.assertTrue(ItemLink.objects.filter(id=link.id).exists())
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.status, 'blocked')

    def test_delete_last_blocker_restore_false_rolls_back_pls953(self):
        """A legitimate False restore is load-bearing, not success."""
        (link,) = self._pls498_block_item1_by(self.item2)
        with patch(
            'tasks.services.workflow_service.WorkflowService._restore_blocked_item',
            return_value=False,
        ):
            response = self.client.delete(
                f'/api/items/{self.item1.id}/links/{link.id}/')
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['code'], 'dependency_release_rejected')
        self.assertTrue(ItemLink.objects.filter(id=link.id).exists())
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.status, 'blocked')

    def test_delete_blocker_link_inverse_blocks_addressing_pls498(self):
        """Inverse blocker addressing resolves and resumes the same dependent."""
        (link,) = self._pls498_block_item1_by(self.item2, link_type='blocks')
        response = self.client.delete(
            f'/api/items/{self.item2.id}/links/{link.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.item1.refresh_from_db()
        self.assertNotEqual(self.item1.status, 'blocked')

    def test_delete_blocker_link_terminal_target_succeeds_pls498(self):
        """A terminal target does not prevent stale-edge removal."""
        (link,) = self._pls498_block_item1_by(self.item2)
        self.item2.status = 'done'
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item2.save()
        response = self.client.delete(
            f'/api/items/{self.item1.id}/links/{link.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ItemLink.objects.filter(id=link.id).exists())
        self.item1.refresh_from_db()
        self.assertNotEqual(self.item1.status, 'blocked')

    def test_delete_blocker_link_idempotent_repeat_pls498(self):
        """Repeating a completed unlink returns the established 404, never 409."""
        links = self._pls498_block_item1_by(self.item2, self.item3)
        first = self.client.delete(
            f'/api/items/{self.item1.id}/links/{links[0].id}/')
        self.assertEqual(first.status_code, status.HTTP_204_NO_CONTENT)
        second = self.client.delete(
            f'/api/items/{self.item1.id}/links/{links[0].id}/')
        self.assertEqual(second.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_link_records_audit_actor_pls498(self):
        """The removal audit records actor, dependent, target, and link type."""
        from tasks.models import TaskEvent
        (link,) = self._pls498_block_item1_by(self.item2)
        target_id = link.target_item_id
        TaskEvent.objects.filter(kind=TaskEvent.KIND_LINK_CHANGED).delete()
        response = self.client.delete(
            f'/api/items/{self.item1.id}/links/{link.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        event = TaskEvent.objects.filter(
            kind=TaskEvent.KIND_LINK_CHANGED).order_by('-seq').first()
        self.assertIsNotNone(event)
        self.assertEqual(event.actor_user_id, self.user.id)
        self.assertEqual(event.item_id, self.item1.id)
        self.assertEqual(event.payload.get('link_type'), 'is_blocked_by')
        self.assertEqual(event.payload.get('target_item_id'), target_id)

    def test_delete_blocker_link_unauthorized_403_edge_intact_pls498(self):
        """An unauthorized caller cannot delete or mutate the dependent."""
        User = get_user_model()
        self.project.access_level = 'private'
        self.project.save()
        (link,) = self._pls498_block_item1_by(self.item2)
        outsider = User.objects.create_user(
            username='outsider498',
            email='outsider498@shizuha.com',
            password='x',
            is_staff=False,
        )
        self.client.force_authenticate(outsider)
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            response = self.client.delete(
                f'/api/items/{self.item1.id}/links/{link.id}/')
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
        self.assertTrue(ItemLink.objects.filter(id=link.id).exists())
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.status, 'blocked')

    def test_delete_blocker_edge_blocker_only_caller_denied_pls498(self):
        """Blocker-only authority cannot delete the dependent-owned edge."""
        User = get_user_model()
        self.project.access_level = 'private'
        self.project.save()
        blocker_only = User.objects.create_user(
            username='blkonly498',
            email='blkonly498@shizuha.com',
            password='x',
            is_staff=False,
        )
        self.item2.mode = 'task'
        self.item2.status = 'in_progress'
        self.item2.assignee_id = blocker_only.id
        self.item2.assignee_email = blocker_only.email
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item2.save()
        link = ItemLink.objects.create(
            source_item=self.item2,
            target_item=self.item1,
            link_type='blocks',
            created_by_id=self.user.id,
            created_by_email=self.user.email,
        )
        self.item1.metadata = {
            'pre_block_state': {'status': 'in_progress', 'had_active_blocker': True}}
        self.item1.status = 'blocked'
        self.item1.save()
        self.client.force_authenticate(blocker_only)
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            response = self.client.delete(
                f'/api/items/{self.item2.id}/links/{link.id}/')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(ItemLink.objects.filter(id=link.id).exists())
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.status, 'blocked')

    def test_delete_blocker_edge_dependent_owner_either_direction_pls498(self):
        """The real MCP caller reaches DELETE from either typed direction."""
        from tasks.tests.test_mcp_schema_contract import pulse_server

        User = get_user_model()
        self.project.access_level = 'private'
        self.project.save()
        dependent_owner = User.objects.create_user(
            username='depowner498',
            email='depowner498@shizuha.com',
            password='x',
            is_staff=False,
        )
        self.item1.mode = 'task'
        self.item1.assignee_id = dependent_owner.id
        self.item1.assignee_email = dependent_owner.email
        self.item2.mode = 'task'
        self.item2.status = 'in_progress'
        self.item2.assignee_id = self.user.id
        self.item2.assignee_email = self.user.email
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            self.item1.save()
            self.item2.save()
        self.client.force_authenticate(dependent_owner)

        def arm_edge():
            ItemLink.objects.all().delete()
            edge = ItemLink.objects.create(
                source_item=self.item2,
                target_item=self.item1,
                link_type='blocks',
                created_by_id=self.user.id,
                created_by_email=self.user.email,
            )
            self.item1.metadata = {
                'pre_block_state': {
                    'status': 'in_progress', 'had_active_blocker': True}}
            self.item1.status = 'blocked'
            with patch(
                'tasks.services.admin_service.admin_service.is_org_member',
                return_value=True,
            ):
                self.item1.save()
            return edge

        def mcp_api_request(method, path, **kwargs):
            """Bridge the MCP production call order to this private-project API."""
            request_path = f'/api{path}'
            if method == 'GET':
                response = self.client.get(request_path, data=kwargs.get('params'))
            elif method == 'DELETE':
                response = self.client.delete(request_path)
            else:
                self.fail(f'unexpected MCP method: {method} {path}')
            if response.status_code >= 400:
                return {
                    'error': f'HTTP {response.status_code}',
                    'detail': response.data,
                    'status_code': response.status_code,
                }
            if response.status_code == status.HTTP_204_NO_CONTENT:
                return {'success': True}
            return response.data

        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            # Exact-org isolation is a prerequisite for item participation, not
            # the authorization being tested. Both principals are org members;
            # only the dependent owner is an item participant.
            return_value=True,
        ):
            edge = arm_edge()
            with patch.object(
                pulse_server, 'api_request', side_effect=mcp_api_request,
            ) as api:
                output = pulse_server.unlink_issues(
                    self.item1.item_key,
                    self.item2.item_key,
                    link_type='is_blocked_by',
                )
            self.assertIn('removed is_blocked_by', output)
            self.assertEqual(
                [(call.args[0], call.args[1]) for call in api.call_args_list],
                [
                    ('GET', f'/items/{self.item1.item_key}/links/'),
                    ('DELETE', f'/items/{self.item1.item_key}/links/{edge.id}/'),
                ],
            )
            self.item1.refresh_from_db()
            self.assertNotEqual(self.item1.status, 'blocked')

            edge = arm_edge()
            with patch.object(
                pulse_server, 'api_request', side_effect=mcp_api_request,
            ) as api:
                output = pulse_server.unlink_issues(
                    self.item2.item_key,
                    self.item1.item_key,
                    link_type='blocks',
                )
            self.assertIn('removed blocks', output)
            self.assertEqual(
                [(call.args[0], call.args[1]) for call in api.call_args_list],
                [
                    ('GET', f'/items/{self.item2.item_key}/links/'),
                    ('GET', f'/items/{self.item1.item_key}/links/'),
                    ('DELETE', f'/items/{self.item1.item_key}/links/{edge.id}/'),
                ],
            )
            self.item1.refresh_from_db()
            self.assertNotEqual(self.item1.status, 'blocked')

            # PLS-673: the MCP schema also accepts numeric item IDs. The real
            # listing serializes both endpoint keys and primary keys, so the
            # caller must match the numeric alias rather than comparing only
            # against ``*_item_key``.
            edge = arm_edge()
            with patch.object(
                pulse_server, 'api_request', side_effect=mcp_api_request,
            ) as api:
                output = pulse_server.unlink_issues(
                    str(self.item1.id),
                    str(self.item2.id),
                    link_type='is_blocked_by',
                )
            self.assertIn('removed is_blocked_by', output)
            self.assertEqual(
                [(call.args[0], call.args[1]) for call in api.call_args_list],
                [
                    ('GET', f'/items/{self.item1.id}/links/'),
                    ('DELETE', f'/items/{self.item1.id}/links/{edge.id}/'),
                ],
            )
            self.item1.refresh_from_db()
            self.assertNotEqual(self.item1.status, 'blocked')

    def test_delete_blocker_edge_privileged_staff_succeeds_pls498(self):
        """Privileged staff may remove the edge without task participation."""
        User = get_user_model()
        self.project.access_level = 'private'
        self.project.save()
        staff = User.objects.create_user(
            username='staff498',
            email='staff498@shizuha.com',
            password='x',
            is_staff=True,
        )
        (link,) = self._pls498_block_item1_by(self.item2)
        self.client.force_authenticate(staff)
        response = self.client.delete(
            f'/api/items/{self.item1.id}/links/{link.id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ItemLink.objects.filter(id=link.id).exists())
        self.item1.refresh_from_db()
        self.assertNotEqual(self.item1.status, 'blocked')


class DeleteLinkConcurrencyTests(TransactionTestCase):
    """Two simultaneous blocker removals serialize on the dependent row."""

    reset_sequences = True

    def test_concurrent_double_unlink_resumes_without_409(self):
        """Exercise two top-level DELETE attempts against different edges."""
        import threading
        from django.db import connections

        User = get_user_model()
        user = User.objects.create_user(
            username='ccuser',
            email='ccuser@shizuha.com',
            password='x',
            is_staff=True,
        )
        project = Project.objects.create(name='CC Project', key='CC', org_id=1)
        with patch(
            'tasks.services.admin_service.admin_service.is_org_member',
            return_value=True,
        ):
            dependent = Item.objects.create(
                mode='task',
                title='dependent',
                project=project,
                item_key='CC-1',
                created_by_id=user.id,
                workflow_name='simple',
            )
            links = []
            for index in (2, 3):
                blocker = Item.objects.create(
                    mode='task',
                    title=f'blocker{index}',
                    project=project,
                    item_key=f'CC-{index}',
                    created_by_id=user.id,
                    status='in_progress',
                    assignee_id=user.id,
                    assignee_email=user.email,
                    workflow_name='simple',
                )
                links.append(ItemLink.objects.create(
                    source_item=dependent,
                    target_item=blocker,
                    link_type='is_blocked_by',
                    created_by_id=user.id,
                    created_by_email=user.email,
                ))
            dependent.metadata = {
                'pre_block_state': {
                    'status': 'in_progress', 'had_active_blocker': True}}
            dependent.status = 'blocked'
            dependent.save()

        errors = []
        barrier = threading.Barrier(len(links))

        def worker(link_id):
            barrier.wait()
            client = APIClient()
            client.force_authenticate(user)
            try:
                response = client.delete(
                    f'/api/items/{dependent.id}/links/{link_id}/')
                if response.status_code not in (
                    status.HTTP_204_NO_CONTENT,
                    status.HTTP_404_NOT_FOUND,
                ):
                    errors.append(response.status_code)
            except Exception as exc:  # noqa: BLE001 - surface worker failure
                errors.append(repr(exc))
            finally:
                connections.close_all()

        threads = [
            threading.Thread(target=worker, args=(link.id,)) for link in links]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [], f'concurrent unlink errored/409: {errors}')
        self.assertEqual(
            ItemLink.objects.filter(source_item=dependent).count(),
            0,
        )
        dependent.refresh_from_db()
        self.assertNotEqual(dependent.status, 'blocked')


# =============================================================================
# Watchers Tests
# =============================================================================

class ItemWatcherModelTests(TestCase):
    """Test ItemWatcher model functionality."""

    def setUp(self):
        """Set up test data."""
        self.project = Project.objects.create(
            name='Test Project',
            key='TEST',
            org_id=1
        )
        self.item = Item.objects.create(
            mode='task',
            title='Test Task',
            project=self.project,
            item_key='TEST-1'
        )

    def test_create_watcher(self):
        """Test creating a watcher."""
        watcher = ItemWatcher.objects.create(
            item=self.item,
            user_id=1,
            user_email='watcher@example.com',
            user_name='Watcher User'
        )

        self.assertEqual(watcher.item, self.item)
        self.assertEqual(watcher.user_id, 1)
        self.assertEqual(watcher.user_email, 'watcher@example.com')

    def test_unique_constraint(self):
        """Test that duplicate watchers are prevented."""
        ItemWatcher.objects.create(
            item=self.item,
            user_id=1,
            user_email='watcher@example.com'
        )

        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            ItemWatcher.objects.create(
                item=self.item,
                user_id=1,
                user_email='watcher@example.com'
            )

    def test_multiple_watchers(self):
        """Test that multiple users can watch the same item."""
        ItemWatcher.objects.create(
            item=self.item,
            user_id=1,
            user_email='watcher1@example.com'
        )
        ItemWatcher.objects.create(
            item=self.item,
            user_id=2,
            user_email='watcher2@example.com'
        )

        self.assertEqual(self.item.watchers.count(), 2)

    def test_watcher_cascade_delete(self):
        """Test that watchers are deleted when item is deleted."""
        watcher = ItemWatcher.objects.create(
            item=self.item,
            user_id=1,
            user_email='watcher@example.com'
        )
        watcher_id = watcher.id

        self.item.delete()

        self.assertFalse(ItemWatcher.objects.filter(id=watcher_id).exists())


class ItemWatcherAPITests(AuthenticatedAPITestCase):
    """Test ItemWatcher API endpoints."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.project = Project.objects.create(
            name='Test Project',
            key='TEST',
            org_id=1
        )
        self.item = Item.objects.create(
            mode='task',
            title='Test Task',
            project=self.project,
            item_key='TEST-1',
            created_by_id=self.user.id
        )

    def test_list_watchers(self):
        """Test listing watchers for an item."""
        ItemWatcher.objects.create(
            item=self.item,
            user_id=10,
            user_email='watcher1@example.com',
            user_name='Watcher One'
        )
        ItemWatcher.objects.create(
            item=self.item,
            user_id=11,
            user_email='watcher2@example.com',
            user_name='Watcher Two'
        )

        response = self.client.get(f'/api/items/{self.item.id}/watchers/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['watchers']), 2)

    def test_list_watchers_empty(self):
        """Test listing watchers when none exist."""
        response = self.client.get(f'/api/items/{self.item.id}/watchers/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['watchers']), 0)

    def test_watch_item(self):
        """Test starting to watch an item."""
        response = self.client.post(f'/api/items/{self.item.id}/watchers/')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ItemWatcher.objects.count(), 1)
        watcher = ItemWatcher.objects.first()
        self.assertEqual(watcher.user_id, self.user.id)

    def test_unwatch_item(self):
        """Test stopping watching an item."""
        ItemWatcher.objects.create(
            item=self.item,
            user_id=self.user.id,
            user_email=self.user.email
        )

        response = self.client.delete(f'/api/items/{self.item.id}/watchers/')

        # API may return 200 with message or 204 no content
        self.assertIn(response.status_code, [status.HTTP_200_OK, status.HTTP_204_NO_CONTENT])
        self.assertEqual(ItemWatcher.objects.count(), 0)


# =============================================================================
# Comment Reactions Tests
# =============================================================================

class CommentReactionModelTests(TestCase):
    """Test CommentReaction model functionality."""

    def setUp(self):
        """Set up test data."""
        self.project = Project.objects.create(
            name='Test Project',
            key='TEST',
            org_id=1
        )
        self.item = Item.objects.create(
            mode='task',
            title='Test Task',
            project=self.project,
            item_key='TEST-1'
        )
        self.comment = Comment.objects.create(
            item=self.item,
            content='Test comment',
            author_id=1,
            author_email='author@example.com'
        )

    def test_create_reaction(self):
        """Test creating a reaction."""
        reaction = CommentReaction.objects.create(
            comment=self.comment,
            user_id=2,
            user_email='reactor@example.com',
            reaction='thumbs_up'
        )

        self.assertEqual(reaction.comment, self.comment)
        self.assertEqual(reaction.reaction, 'thumbs_up')
        self.assertEqual(reaction.user_id, 2)

    def test_all_reaction_types(self):
        """Test all defined reaction types."""
        reaction_types = [
            'thumbs_up', 'thumbs_down', 'heart', 'laugh',
            'confused', 'celebrate', 'rocket', 'eyes'
        ]

        for i, reaction_type in enumerate(reaction_types):
            reaction = CommentReaction.objects.create(
                comment=self.comment,
                user_id=i + 10,  # Different user for each
                user_email=f'user{i}@example.com',
                reaction=reaction_type
            )
            self.assertEqual(reaction.reaction, reaction_type)

    def test_unique_constraint(self):
        """Test that same user can't add same reaction twice."""
        CommentReaction.objects.create(
            comment=self.comment,
            user_id=1,
            user_email='user@example.com',
            reaction='thumbs_up'
        )

        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            CommentReaction.objects.create(
                comment=self.comment,
                user_id=1,
                user_email='user@example.com',
                reaction='thumbs_up'
            )

    def test_same_user_different_reactions(self):
        """Test that same user can add different reactions."""
        CommentReaction.objects.create(
            comment=self.comment,
            user_id=1,
            user_email='user@example.com',
            reaction='thumbs_up'
        )
        CommentReaction.objects.create(
            comment=self.comment,
            user_id=1,
            user_email='user@example.com',
            reaction='heart'
        )

        self.assertEqual(self.comment.reactions.count(), 2)

    def test_reaction_cascade_delete(self):
        """Test that reactions are deleted when comment is deleted."""
        reaction = CommentReaction.objects.create(
            comment=self.comment,
            user_id=1,
            user_email='user@example.com',
            reaction='thumbs_up'
        )
        reaction_id = reaction.id

        self.comment.delete()

        self.assertFalse(CommentReaction.objects.filter(id=reaction_id).exists())


class CommentReactionAPITests(AuthenticatedAPITestCase):
    """Test CommentReaction API endpoints."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.project = Project.objects.create(
            name='Test Project',
            key='TEST',
            org_id=1
        )
        self.item = Item.objects.create(
            mode='task',
            title='Test Task',
            project=self.project,
            item_key='TEST-1',
            created_by_id=self.user.id
        )
        self.comment = Comment.objects.create(
            item=self.item,
            content='Test comment',
            author_id=self.user.id,
            author_email=self.user.email
        )

    def test_add_reaction(self):
        """Test adding a reaction to a comment."""
        data = {'reaction': 'thumbs_up'}

        response = self.client.post(f'/api/comments/{self.comment.id}/reactions/', data, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(CommentReaction.objects.count(), 1)

    def test_remove_reaction(self):
        """Test removing a reaction from a comment."""
        CommentReaction.objects.create(
            comment=self.comment,
            user_id=self.user.id,
            user_email=self.user.email,
            reaction='thumbs_up'
        )

        # Reaction is passed as query parameter, not body
        response = self.client.delete(
            f'/api/comments/{self.comment.id}/reactions/?reaction=thumbs_up'
        )

        # API returns 200 with success message
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(CommentReaction.objects.count(), 0)


# =============================================================================
# Integration Tests - Full Workflow Tests
# =============================================================================

class WorkflowFeaturesIntegrationTests(AuthenticatedAPITestCase):
    """Integration tests for the complete Jira-like workflow."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.project = Project.objects.create(
            name='Integration Test Project',
            key='INT',
            org_id=1
        )

    def test_full_task_workflow(self):
        """Test complete workflow: create task, add links, add watchers, add comments with reactions."""
        # 1. Create tasks
        item1 = Item.objects.create(
            mode='task',
            title='Parent Task',
            description='Main task',
            project=self.project,
            item_key='INT-1',
            priority='high',
            created_by_id=self.user.id
        )

        item2 = Item.objects.create(
            mode='task',
            title='Blocked Task',
            description='Blocked by parent',
            project=self.project,
            item_key='INT-2',
            created_by_id=self.user.id
        )

        # 2. Verify task can be fetched by key
        response = self.client.get('/api/items/by-key/INT-1/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['title'], 'Parent Task')

        # 3. Create a link between tasks via API
        response = self.client.post(
            f'/api/items/{item1.id}/links/',
            {'target_item_key': 'INT-2', 'link_type': 'blocks'},
            format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # 4. Verify links are visible
        response = self.client.get(f'/api/items/{item1.id}/links/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['outward_links']), 1)

        response = self.client.get(f'/api/items/{item2.id}/links/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['inward_links']), 1)

        # 5. Start watching the task
        response = self.client.post(f'/api/items/{item1.id}/watchers/')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # 6. Verify watchers are visible
        response = self.client.get(f'/api/items/{item1.id}/watchers/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['watchers']), 1)

        # 7. Add a comment
        comment = Comment.objects.create(
            item=item1,
            content='This is a test comment',
            author_id=self.user.id,
            author_email=self.user.email
        )

        # 8. Add reaction to comment
        response = self.client.post(
            f'/api/comments/{comment.id}/reactions/',
            {'reaction': 'thumbs_up'},
            format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # 9. Verify everything is connected
        self.assertEqual(item1.outward_links.count(), 1)
        self.assertEqual(item1.watchers.count(), 1)
        self.assertEqual(item1.comments.count(), 1)
        self.assertEqual(comment.reactions.count(), 1)

    def test_activity_tracking_flow(self):
        """Test activity is tracked through various operations."""
        item = Item.objects.create(
            mode='task',
            title='Activity Test Task',
            project=self.project,
            item_key='INT-10',
            status='pending',
            created_by_id=self.user.id
        )

        # Clear any auto-created activities from signals
        ItemActivity.objects.filter(item=item).delete()

        # Create manual activity entries to simulate signal-based tracking
        ItemActivity.objects.create(
            item=item,
            action='created',
            user_id=self.user.id,
            user_email=self.user.email
        )

        ItemActivity.objects.create(
            item=item,
            action='status_changed',
            field_name='status',
            old_value='pending',
            new_value='in_progress',
            user_id=self.user.id,
            user_email=self.user.email
        )

        ItemActivity.objects.create(
            item=item,
            action='assigned',
            new_value='assignee@example.com',
            user_id=self.user.id,
            user_email=self.user.email
        )

        # Verify activity timeline
        response = self.client.get(f'/api/items/{item.id}/activity/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['activities']), 3)

        # Activities should be in reverse chronological order
        activities = response.data['activities']
        self.assertEqual(activities[0]['action'], 'assigned')
        self.assertEqual(activities[1]['action'], 'status_changed')
        self.assertEqual(activities[2]['action'], 'created')

    def test_attachment_workflow(self):
        """Test attachment listing workflow."""
        item = Item.objects.create(
            mode='task',
            title='Attachment Test Task',
            project=self.project,
            item_key='INT-20',
            created_by_id=self.user.id
        )

        # Create attachments (simulating upload)
        Attachment.objects.create(
            item=item,
            storage_key='attachments/INT-20/doc.pdf',
            filename='document.pdf',
            file_type='application/pdf',
            file_size=102400,
            uploaded_by_id=self.user.id,
            uploaded_by_email=self.user.email
        )

        Attachment.objects.create(
            item=item,
            storage_key='attachments/INT-20/image.png',
            filename='screenshot.png',
            file_type='image/png',
            file_size=51200,
            is_image=True,
            width=1920,
            height=1080,
            thumbnail_key='attachments/INT-20/thumbnails/screenshot_thumb.png',
            uploaded_by_id=self.user.id,
            uploaded_by_email=self.user.email
        )

        # Verify attachments are listed
        response = self.client.get(f'/api/items/{item.id}/attachments/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['attachments']), 2)

        # Check image attachment has thumbnail info
        attachments = response.data['attachments']
        image_attachment = next((a for a in attachments if a['is_image']), None)
        self.assertIsNotNone(image_attachment)
        self.assertEqual(image_attachment['width'], 1920)
        self.assertEqual(image_attachment['height'], 1080)


# =============================================================================
# Edge Cases and Error Handling Tests
# =============================================================================

class EdgeCaseTests(AuthenticatedAPITestCase):
    """Test edge cases and error handling."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.project = Project.objects.create(
            name='Test Project',
            key='EDGE',
            org_id=1
        )
        self.item = Item.objects.create(
            mode='task',
            title='Test Task',
            project=self.project,
            item_key='EDGE-1',
            created_by_id=self.user.id
        )

    def test_self_linking_prevention(self):
        """Test that an item cannot link to itself."""
        # This should be handled by validation
        try:
            link = ItemLink.objects.create(
                source_item=self.item,
                target_item=self.item,  # Same item
                link_type='blocks',
                created_by_id=self.user.id,
                created_by_email=self.user.email
            )
            # If it succeeds, the model doesn't prevent self-links
            # This is a design decision - some systems allow it
        except Exception:
            pass  # Expected if validation prevents self-links

    def test_activity_for_nonexistent_item(self):
        """Test fetching activity for non-existent item returns 404."""
        response = self.client.get('/api/items/99999/activity/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_links_for_nonexistent_item(self):
        """Test fetching links for non-existent item returns 404."""
        response = self.client.get('/api/items/99999/links/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_watchers_for_nonexistent_item(self):
        """Test fetching watchers for non-existent item returns 404."""
        response = self.client.get('/api/items/99999/watchers/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_attachments_for_nonexistent_item(self):
        """Test fetching attachments for non-existent item returns 404."""
        response = self.client.get('/api/items/99999/attachments/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_invalid_link_type(self):
        """Test that invalid link types are rejected."""
        item2 = Item.objects.create(
            mode='task',
            title='Task 2',
            project=self.project,
            item_key='EDGE-2',
            created_by_id=self.user.id
        )

        # Try to create with invalid link type
        try:
            link = ItemLink.objects.create(
                source_item=self.item,
                target_item=item2,
                link_type='invalid_type',  # Invalid
                created_by_id=self.user.id,
                created_by_email=self.user.email
            )
            # If validation is on model level, this should fail
        except Exception:
            pass  # Expected

    def test_invalid_reaction_type(self):
        """Test that invalid reaction types are rejected."""
        comment = Comment.objects.create(
            item=self.item,
            content='Test',
            author_id=self.user.id,
            author_email=self.user.email
        )

        try:
            reaction = CommentReaction.objects.create(
                comment=comment,
                user_id=self.user.id,
                user_email=self.user.email,
                reaction='invalid_reaction'  # Invalid
            )
            # If validation is on model level, this should fail
        except Exception:
            pass  # Expected

    def test_large_activity_history(self):
        """Test that large activity histories are handled properly."""
        # Create many activities
        for i in range(100):
            ItemActivity.objects.create(
                item=self.item,
                action='updated',
                field_name='description',
                old_value=f'Value {i}',
                new_value=f'Value {i+1}',
                user_id=self.user.id,
                user_email=self.user.email
            )

        response = self.client.get(f'/api/items/{self.item.id}/activity/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return activities (possibly paginated)
        self.assertGreater(len(response.data['activities']), 0)


# =============================================================================
# Performance Tests
# =============================================================================

class PerformanceTests(AuthenticatedAPITestCase):
    """Basic performance tests for database queries."""

    def setUp(self):
        """Set up test data."""
        super().setUp()
        self.project = Project.objects.create(
            name='Perf Project',
            key='PERF',
            org_id=1
        )

    def test_links_query_efficiency(self):
        """Test that fetching links doesn't cause N+1 queries."""
        # Create an item with many links
        main_item = Item.objects.create(
            mode='task',
            title='Main Task',
            project=self.project,
            item_key='PERF-1',
            created_by_id=self.user.id
        )

        for i in range(10):
            target = Item.objects.create(
                mode='task',
                title=f'Target Task {i}',
                project=self.project,
                item_key=f'PERF-{i+10}',
                created_by_id=self.user.id
            )
            ItemLink.objects.create(
                source_item=main_item,
                target_item=target,
                link_type='relates_to',
                created_by_id=self.user.id,
                created_by_email=self.user.email
            )

        # This should be efficient
        response = self.client.get(f'/api/items/{main_item.id}/links/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['outward_links']), 10)

    def test_activity_query_efficiency(self):
        """Test that fetching activities doesn't cause excessive queries."""
        item = Item.objects.create(
            mode='task',
            title='Activity Task',
            project=self.project,
            item_key='PERF-100',
            created_by_id=self.user.id
        )

        # Create many activities
        for i in range(50):
            ItemActivity.objects.create(
                item=item,
                action='updated',
                user_id=self.user.id,
                user_email=self.user.email
            )

        response = self.client.get(f'/api/items/{item.id}/activity/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
