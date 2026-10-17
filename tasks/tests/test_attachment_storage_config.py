import io
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from minio.error import S3Error

from tasks.services.storage import (
    AttachmentStorage,
    AttachmentStorageAuthenticationError,
    AttachmentStorageConfigurationError,
)


class AttachmentStorageConfigurationTests(SimpleTestCase):
    def test_missing_configuration_fails_before_client_construction(self):
        storage = AttachmentStorage(endpoint='', access_key='', secret_key='', bucket='')

        with patch('tasks.services.storage.Minio') as constructor:
            with self.assertRaisesRegex(
                AttachmentStorageConfigurationError,
                'MINIO_ACCESS_KEY, MINIO_ENDPOINT, MINIO_PULSE_BUCKET, MINIO_SECRET_KEY',
            ):
                _ = storage.client

        constructor.assert_not_called()

    def test_well_known_development_credentials_are_rejected(self):
        storage = AttachmentStorage(
            endpoint='minio.shizuha.svc.cluster.local:9000',
            access_key='minioadmin',
            secret_key='minioadmin',
            bucket='pulse-attachments',
        )

        with patch('tasks.services.storage.Minio') as constructor:
            with self.assertRaisesRegex(
                AttachmentStorageConfigurationError,
                'rejects development credentials',
            ):
                _ = storage.client

        constructor.assert_not_called()

    def test_configured_secret_values_build_client_and_check_bucket(self):
        client = MagicMock()
        client.bucket_exists.return_value = True
        storage = AttachmentStorage(
            endpoint='minio.shizuha.svc.cluster.local:9000',
            access_key='configured-access',
            secret_key='configured-secret',
            bucket='pulse-attachments',
        )

        with patch('tasks.services.storage.Minio', return_value=client) as constructor:
            self.assertIs(storage.client, client)

        constructor.assert_called_once_with(
            'minio.shizuha.svc.cluster.local:9000',
            access_key='configured-access',
            secret_key='configured-secret',
            secure=False,
        )
        client.bucket_exists.assert_called_once_with('pulse-attachments')

    def test_signature_mismatch_is_a_safe_authentication_failure(self):
        client = MagicMock()
        client.bucket_exists.side_effect = S3Error(
            MagicMock(status=403),
            'SignatureDoesNotMatch',
            'the request signature does not match',
            'pulse-attachments',
            'request-id',
            'host-id',
        )
        storage = AttachmentStorage(
            endpoint='minio.shizuha.svc.cluster.local:9000',
            access_key='stale-access',
            secret_key='stale-secret',
            bucket='pulse-attachments',
        )

        with patch('tasks.services.storage.Minio', return_value=client):
            with self.assertRaises(AttachmentStorageAuthenticationError) as caught:
                _ = storage.client

        self.assertNotIn('stale-access', str(caught.exception))
        self.assertNotIn('stale-secret', str(caught.exception))

    def test_configured_bucket_is_used_for_upload(self):
        storage = AttachmentStorage(
            endpoint='minio.example:9000', access_key='access', secret_key='secret',
            bucket='configured-attachments',
        )
        storage._client = MagicMock()

        storage.upload_file(
            io.BytesIO(b'payload'), 'file.txt', 'text/plain', item_id=7, user_id=9,
        )

        self.assertEqual(storage._client.put_object.call_args.args[0], 'configured-attachments')


class AttachmentPublicEndpointTests(SimpleTestCase):
    def make_storage(self, **kwargs):
        storage = AttachmentStorage(
            endpoint='storage.internal:9000', access_key='configured-access',
            secret_key='configured-secret', bucket='pulse-attachments', **kwargs,
        )
        storage._client = MagicMock()
        storage._client._get_region.return_value = 'us-east-1'
        return storage

    def test_default_uses_existing_internal_client(self):
        storage = self.make_storage(public_endpoint='', public_secure=False)
        storage.get_download_url('objects/example.txt')
        storage._client.presigned_get_object.assert_called_once()
        storage._client._get_region.assert_not_called()

    def test_download_and_thumbnail_sign_public_host_without_public_network(self):
        from urllib.parse import parse_qs, urlsplit
        storage = self.make_storage(public_endpoint='storage.example.test:9443', public_secure=True)
        with patch('urllib3.PoolManager.urlopen', side_effect=AssertionError('public network forbidden')):
            for url in (storage.get_download_url('objects/example.txt'),
                        storage.get_thumbnail_url('thumbnails/example.png')):
                parsed = urlsplit(url)
                self.assertEqual(parsed.scheme, 'https')
                self.assertEqual(parsed.netloc, 'storage.example.test:9443')
                self.assertIn('X-Amz-Signature', parse_qs(parsed.query))
                self.assertEqual(parse_qs(parsed.query)['X-Amz-SignedHeaders'], ['host'])
        storage._client._get_region.assert_called_once_with('pulse-attachments')
        storage._client.presigned_get_object.assert_not_called()
        self.assertEqual(storage.endpoint, 'storage.internal:9000')

    def test_environment_configures_public_endpoint_and_tls_independently(self):
        with patch.dict('os.environ', {
            'MINIO_PUBLIC_ENDPOINT': 'localhost:8333', 'MINIO_PUBLIC_SECURE': 'false',
        }):
            storage = self.make_storage(secure=True)
        self.assertEqual(storage.public_endpoint, 'localhost:8333')
        self.assertFalse(storage.public_secure)
        self.assertTrue(storage.secure)
