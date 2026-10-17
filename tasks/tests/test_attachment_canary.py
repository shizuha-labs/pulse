from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

from django.test import SimpleTestCase
from minio import Minio
from minio.error import S3Error
from rest_framework import status

from tasks.services.attachment_canary import (
    AttachmentCanaryFailure,
    CANARY_CONTENT_TYPE,
    CANARY_PAYLOAD,
    CANARY_PREFIX,
    EXPECTED_MINIO_BUCKET,
    EXPECTED_MINIO_ENDPOINT,
    EXPECTED_MINIO_REGION,
    run_attachment_storage_canary,
)
from tasks.services.storage import (
    AttachmentStorage,
    AttachmentStorageAuthenticationError,
    AttachmentStorageConfigurationError,
)


def missing_key_error():
    return S3Error(
        MagicMock(status=404),
        'NoSuchKey',
        'not found',
        'pulse-attachments',
        'request-id',
        'host-id',
    )


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.closed = False
        self.released = False

    def read(self, amount=None):
        return self.payload if amount is None else self.payload[:amount]

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


class FakeClient:
    def __init__(self):
        self.objects = {}
        self.stale = []
        self.last_response = None
        self.corrupt_read = False
        self.fail_delete = False
        self.bucket_absent = False
        self.region = EXPECTED_MINIO_REGION

    def bucket_exists(self, bucket):
        # PLS-483 topology phase: the canary verifies the bucket EXISTS (it never
        # auto-creates). `bucket_absent=True` models a config drift to a
        # non-existent bucket.
        return not self.bucket_absent

    def list_objects(self, bucket, prefix, recursive):
        assert bucket == 'pulse-attachments'
        assert prefix == CANARY_PREFIX
        assert recursive is True
        return list(self.stale)

    def put_object(self, bucket, key, stream, length, content_type, metadata):
        payload = stream.read()
        assert length == len(payload)
        self.objects[key] = {
            'payload': payload,
            'content_type': content_type,
            'metadata': {f'x-amz-meta-{k}': v for k, v in metadata.items()},
        }

    def stat_object(self, bucket, key):
        if key not in self.objects:
            raise missing_key_error()
        obj = self.objects[key]
        return SimpleNamespace(
            size=len(obj['payload']),
            content_type=obj['content_type'],
            metadata=obj['metadata'],
        )

    def get_object(self, bucket, key):
        payload = self.objects[key]['payload']
        if self.corrupt_read:
            payload += b'corrupt'
        self.last_response = FakeResponse(payload)
        return self.last_response

    def remove_object(self, bucket, key):
        if self.fail_delete:
            raise RuntimeError('delete failed')
        self.objects.pop(key, None)


class FakeStorage:
    def __init__(self, client):
        self.client = client
        self.endpoint = EXPECTED_MINIO_ENDPOINT
        self.bucket = EXPECTED_MINIO_BUCKET

    def get_bucket_region(self):
        return self.client.region


def fake_storage(client):
    return FakeStorage(client)


class AttachmentStorageCanaryTests(SimpleTestCase):
    def test_region_adapter_executes_real_locked_minio_consumer_boundary(self):
        storage = AttachmentStorage(
            endpoint=EXPECTED_MINIO_ENDPOINT,
            access_key='test-access',
            secret_key='test-secret',
            bucket=EXPECTED_MINIO_BUCKET,
            secure=False,
            ensure_bucket=False,
        )
        client = storage.client
        self.assertIsInstance(client, Minio)
        response = SimpleNamespace(data=(
            b'<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            b'us-east-1</LocationConstraint>'
        ))

        with patch.object(client, '_url_open', return_value=response) as request:
            self.assertEqual(storage.get_bucket_region(), EXPECTED_MINIO_REGION)

        request.assert_called_once_with(
            'GET',
            'us-east-1',
            bucket_name=EXPECTED_MINIO_BUCKET,
            query_params={'location': ''},
        )

    def test_region_adapter_rejects_missing_or_incompatible_sdk_resolver(self):
        storage = AttachmentStorage(
            endpoint=EXPECTED_MINIO_ENDPOINT,
            access_key='test-access',
            secret_key='test-secret',
            bucket=EXPECTED_MINIO_BUCKET,
            ensure_bucket=False,
        )
        storage._client = SimpleNamespace(_get_region=None)
        with self.assertRaises(AttachmentStorageConfigurationError):
            storage.get_bucket_region()

        storage._client = SimpleNamespace(_get_region=lambda: EXPECTED_MINIO_REGION)
        with self.assertRaises(AttachmentStorageConfigurationError):
            storage.get_bucket_region()

    def test_exact_create_read_validate_delete_and_metadata(self):
        client = FakeClient()

        result = run_attachment_storage_canary(storage=fake_storage(client))

        self.assertTrue(result['ok'])
        self.assertTrue(all(result['phases'].values()))
        self.assertEqual(result['payload_size'], len(CANARY_PAYLOAD))
        self.assertEqual(client.objects, {})
        self.assertTrue(client.last_response.closed)
        self.assertTrue(client.last_response.released)

    def test_stale_cleanup_is_prefix_scoped_and_bounded(self):
        client = FakeClient()
        now = datetime.now(timezone.utc)
        stale_name = CANARY_PREFIX + 'stale.bin'
        fresh_name = CANARY_PREFIX + 'fresh.bin'
        client.objects[stale_name] = {'payload': b'x', 'content_type': CANARY_CONTENT_TYPE, 'metadata': {}}
        client.objects[fresh_name] = {'payload': b'x', 'content_type': CANARY_CONTENT_TYPE, 'metadata': {}}
        client.stale = [
            SimpleNamespace(object_name=stale_name, last_modified=now - timedelta(hours=2)),
            SimpleNamespace(object_name=fresh_name, last_modified=now),
        ]

        result = run_attachment_storage_canary(storage=fake_storage(client), now=now)

        self.assertEqual(result['stale_objects_removed'], 1)
        self.assertNotIn(stale_name, client.objects)
        self.assertIn(fresh_name, client.objects)

    def test_read_write_asymmetry_is_typed_and_finally_cleans(self):
        client = FakeClient()
        client.corrupt_read = True

        with self.assertRaises(AttachmentCanaryFailure) as caught:
            run_attachment_storage_canary(storage=fake_storage(client))

        self.assertEqual(caught.exception.phase, 'validate')
        self.assertEqual(caught.exception.reason, 'content_mismatch')
        self.assertEqual(client.objects, {})

    def test_delete_failure_is_typed_and_leaves_ttl_fallback_object(self):
        client = FakeClient()
        client.fail_delete = True

        with self.assertRaises(AttachmentCanaryFailure) as caught:
            run_attachment_storage_canary(storage=fake_storage(client))

        self.assertEqual(caught.exception.phase, 'delete')
        self.assertEqual(caught.exception.reason, 'cleanup_failed')
        self.assertEqual(len(client.objects), 1)
        self.assertTrue(next(iter(client.objects)).startswith(CANARY_PREFIX))

    @patch('tasks.services.attachment_canary.AttachmentStorage')
    @patch('tasks.services.attachment_canary.attachment_storage')
    def test_signature_fault_uses_isolated_bad_client(self, live_storage, constructor):
        live_storage.endpoint = 'minio.example:9000'
        live_storage.access_key = 'live-access'
        live_storage.bucket = 'pulse-attachments'
        live_storage.secure = False
        bad = MagicMock()
        type(bad).client = property(lambda _self: (_ for _ in ()).throw(
            AttachmentStorageAuthenticationError('signature mismatch')
        ))
        constructor.return_value = bad

        with self.assertRaises(AttachmentCanaryFailure) as caught:
            run_attachment_storage_canary(fault_mode='signature')

        self.assertEqual(caught.exception.phase, 'auth')
        self.assertEqual(caught.exception.reason, 'signature_mismatch')

        constructor.assert_called_once_with(
            endpoint='minio.example:9000',
            access_key='live-access',
            secret_key='pls483-intentional-invalid-secret',
            bucket='pulse-attachments',
            secure=False,
            http_client=ANY,       # bounded canary client (review P1 #1)
            ensure_bucket=False,   # never auto-create (review P1 #3)
        )

    def test_topology_drift_bucket_missing_is_typed(self):
        # review P1 #3: a drift to a wrong-but-creatable bucket must FAIL as
        # bucket_missing (the canary never auto-creates), not self-heal + pass.
        client = FakeClient()
        client.bucket_absent = True

        with self.assertRaises(AttachmentCanaryFailure) as caught:
            run_attachment_storage_canary(storage=fake_storage(client))

        self.assertEqual(caught.exception.phase, 'topology')
        self.assertEqual(caught.exception.reason, 'bucket_missing')
        # It failed BEFORE any create — nothing was written or auto-created.
        self.assertEqual(client.objects, {})

    def test_topology_drift_wrong_reachable_endpoint_fails_before_write(self):
        client = FakeClient()
        storage = fake_storage(client)
        storage.endpoint = 'wrong-but-reachable.example:9000'

        with self.assertRaises(AttachmentCanaryFailure) as caught:
            run_attachment_storage_canary(storage=storage)

        self.assertEqual(caught.exception.phase, 'topology')
        self.assertEqual(caught.exception.reason, 'endpoint_mismatch')
        self.assertEqual(client.objects, {})

    def test_topology_drift_wrong_reachable_bucket_fails_before_write(self):
        client = FakeClient()
        storage = fake_storage(client)
        storage.bucket = 'wrong-but-existing-bucket'

        with self.assertRaises(AttachmentCanaryFailure) as caught:
            run_attachment_storage_canary(storage=storage)

        self.assertEqual(caught.exception.phase, 'topology')
        self.assertEqual(caught.exception.reason, 'bucket_mismatch')
        self.assertEqual(client.objects, {})

    def test_topology_drift_wrong_reachable_region_fails_before_write(self):
        client = FakeClient()
        client.region = 'eu-west-1'

        with self.assertRaises(AttachmentCanaryFailure) as caught:
            run_attachment_storage_canary(storage=fake_storage(client))

        self.assertEqual(caught.exception.phase, 'topology')
        self.assertEqual(caught.exception.reason, 'region_mismatch')
        self.assertEqual(client.objects, {})

    def test_topology_sdk_incompatibility_is_typed_before_any_sdk_request(self):
        incompatible_resolvers = (
            ('missing', None),
            ('wrong-signature', lambda: EXPECTED_MINIO_REGION),
        )

        for name, resolver in incompatible_resolvers:
            with self.subTest(name=name):
                storage = AttachmentStorage(
                    endpoint=EXPECTED_MINIO_ENDPOINT,
                    access_key='test-access',
                    secret_key='test-secret',
                    bucket=EXPECTED_MINIO_BUCKET,
                    secure=False,
                    ensure_bucket=False,
                )
                client = storage.client
                self.assertIsInstance(client, Minio)
                client._get_region = resolver

                with (
                    patch.object(client, 'bucket_exists') as bucket_exists,
                    patch.object(client, 'put_object') as put_object,
                    self.assertRaises(AttachmentCanaryFailure) as caught,
                ):
                    run_attachment_storage_canary(storage=storage)

                self.assertEqual(caught.exception.phase, 'topology')
                self.assertEqual(caught.exception.reason, 'configuration')
                bucket_exists.assert_not_called()
                put_object.assert_not_called()
