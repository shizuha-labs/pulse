"""
MinIO storage service for file attachments.

Uses the same MinIO instance as shizuha-drive with a dedicated bucket
for Pulse attachments.
"""

import os
import io
import logging
from typing import Optional, Tuple, BinaryIO
from datetime import timedelta
from minio import Minio
from minio.error import S3Error
from django.conf import settings

logger = logging.getLogger(__name__)

# MinIO Configuration.  Production attachment storage must never silently fall
# back to MinIO's well-known development credential.  Configuration validation
# is lazy so management commands/tests that do not use attachments can still
# import the application; the first storage operation fails closed with a
# non-secret diagnostic naming the missing variables.
MINIO_ENDPOINT = os.environ.get('MINIO_ENDPOINT', '').strip()
MINIO_ACCESS_KEY = os.environ.get('MINIO_ACCESS_KEY', '').strip()
MINIO_SECRET_KEY = os.environ.get('MINIO_SECRET_KEY', '').strip()
MINIO_SECURE = os.environ.get('MINIO_SECURE', 'false').lower() == 'true'
MINIO_BUCKET = os.environ.get('MINIO_PULSE_BUCKET', 'pulse-attachments')

_INSECURE_DEVELOPMENT_CREDENTIALS = {'minioadmin'}
_AUTH_ERROR_CODES = {'SignatureDoesNotMatch', 'InvalidAccessKeyId', 'AccessDenied'}


class AttachmentStorageConfigurationError(RuntimeError):
    """Attachment storage is missing required production configuration."""


class AttachmentStorageAuthenticationError(RuntimeError):
    """Configured attachment-storage credentials were rejected."""

# Thumbnail settings
THUMBNAIL_MAX_SIZE = (200, 200)
THUMBNAIL_PREFIX = 'thumbnails/'

# Allowed image types for thumbnail generation
IMAGE_TYPES = {
    'image/jpeg': '.jpg',
    'image/png': '.png',
    'image/gif': '.gif',
    'image/webp': '.webp',
}


class AttachmentStorage:
    """
    Service for storing and retrieving file attachments in MinIO.
    """

    def __init__(
        self,
        *,
        endpoint: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        bucket: Optional[str] = None,
        secure: Optional[bool] = None,
        http_client: Optional[object] = None,
        ensure_bucket: bool = True,
        public_endpoint: Optional[str] = None,
        public_secure: Optional[bool] = None,
    ):
        self._client = None
        self._presign_client = None
        self.endpoint = MINIO_ENDPOINT if endpoint is None else str(endpoint).strip()
        self.access_key = MINIO_ACCESS_KEY if access_key is None else str(access_key).strip()
        self.secret_key = MINIO_SECRET_KEY if secret_key is None else str(secret_key).strip()
        self.bucket = MINIO_BUCKET if bucket is None else str(bucket).strip()
        self.secure = MINIO_SECURE if secure is None else bool(secure)
        self.public_endpoint = (
            os.environ.get('MINIO_PUBLIC_ENDPOINT', '').strip()
            if public_endpoint is None else str(public_endpoint).strip()
        ) or self.endpoint
        public_secure_env = os.environ.get('MINIO_PUBLIC_SECURE', '').strip()
        self.public_secure = (
            (public_secure_env.lower() == 'true' if public_secure_env else self.secure)
            if public_secure is None else bool(public_secure)
        )
        # PLS-483: an optional caller-supplied urllib3 client lets the canary bound
        # its own connect/read timeouts + retries WITHOUT changing the shared
        # production client (MinIO-SDK default 300s + retries). A canary-scoped
        # instance also disables auto-create so a bucket/endpoint DRIFT is DETECTED
        # as bucket_missing instead of self-healing into a new empty bucket. Both
        # default to the pre-existing production behavior.
        self._http_client = http_client
        self._ensure_bucket_on_connect = bool(ensure_bucket)

    def _validate_configuration(self) -> None:
        values = {
            'MINIO_ENDPOINT': self.endpoint,
            'MINIO_ACCESS_KEY': self.access_key,
            'MINIO_SECRET_KEY': self.secret_key,
            'MINIO_PULSE_BUCKET': self.bucket,
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise AttachmentStorageConfigurationError(
                'Pulse attachment storage is not configured; missing: '
                + ', '.join(sorted(missing))
            )
        insecure = []
        if self.access_key.lower() in _INSECURE_DEVELOPMENT_CREDENTIALS:
            insecure.append('MINIO_ACCESS_KEY')
        if self.secret_key.lower() in _INSECURE_DEVELOPMENT_CREDENTIALS:
            insecure.append('MINIO_SECRET_KEY')
        if insecure:
            raise AttachmentStorageConfigurationError(
                'Pulse attachment storage rejects development credentials: '
                + ', '.join(sorted(insecure))
            )

    @staticmethod
    def _raise_storage_error(error: S3Error, operation: str) -> None:
        code = str(getattr(error, 'code', '') or '')
        if code in _AUTH_ERROR_CODES:
            logger.error('MinIO attachment %s authentication failed code=%s', operation, code)
            raise AttachmentStorageAuthenticationError(
                'MinIO rejected the configured Pulse attachment credentials'
            ) from error
        raise error

    @property
    def client(self) -> Minio:
        """Lazy-load MinIO client."""
        if self._client is None:
            self._validate_configuration()
            minio_kwargs = dict(
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=self.secure,
            )
            if self._http_client is not None:
                minio_kwargs['http_client'] = self._http_client
            self._client = Minio(self.endpoint, **minio_kwargs)
            if self._ensure_bucket_on_connect:
                self._ensure_bucket()
        return self._client

    @property
    def presign_client(self) -> Minio:
        """Sign the browser endpoint without contacting it or rewriting signed URLs."""
        if self.public_endpoint == self.endpoint and self.public_secure == self.secure:
            return self.client
        if self._presign_client is None:
            self._validate_configuration()
            # Resolve the actual bucket region over the internal connection.
            # Supplying it explicitly prevents MinIO from probing the public
            # address, which may only be reachable from the user's browser.
            region = self.get_bucket_region()
            if not region:
                raise AttachmentStorageConfigurationError('MinIO bucket region is empty')
            self._presign_client = Minio(
                self.public_endpoint,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=self.public_secure,
                region=region,
            )
        return self._presign_client

    def get_bucket_region(self) -> str:
        """Return the bucket region through minio-py's signed resolver.

        minio-py 7.2.20 does not expose the boto3-style
        ``get_bucket_location`` API.  Keep that SDK-specific boundary here so
        the canary calls a stable storage-adapter contract, while a future SDK
        incompatibility fails as typed configuration instead of ``unexpected``.
        """
        resolver = getattr(self.client, '_get_region', None)
        if not callable(resolver):
            raise AttachmentStorageConfigurationError(
                'MinIO client does not expose a compatible bucket-region resolver'
            )
        try:
            return str(resolver(self.bucket) or '').strip()
        except (AttributeError, TypeError) as error:
            raise AttachmentStorageConfigurationError(
                'MinIO client bucket-region resolver is incompatible'
            ) from error

    def _ensure_bucket(self):
        """Ensure the attachments bucket exists."""
        try:
            if not self._client.bucket_exists(self.bucket):
                self._client.make_bucket(self.bucket)
                logger.info(f"Created MinIO bucket: {self.bucket}")
        except S3Error as e:
            self._raise_storage_error(e, 'bucket-check')

    def upload_file(
        self,
        file_data: BinaryIO,
        filename: str,
        content_type: str,
        item_id: int,
        user_id: int,
    ) -> Tuple[str, int, Optional[dict]]:
        """
        Upload a file to MinIO.

        Args:
            file_data: File-like object with the data
            filename: Original filename
            content_type: MIME type
            item_id: ID of the item this attachment belongs to
            user_id: ID of the uploading user

        Returns:
            Tuple of (storage_key, file_size, image_metadata)
            image_metadata contains width, height, thumbnail_key for images
        """
        import uuid

        # Generate unique storage key
        file_ext = os.path.splitext(filename)[1].lower()
        unique_id = str(uuid.uuid4())
        storage_key = f"items/{item_id}/{unique_id}{file_ext}"

        # Get file size by seeking to end
        file_data.seek(0, 2)
        file_size = file_data.tell()
        file_data.seek(0)

        # Upload the file
        try:
            self.client.put_object(
                self.bucket,
                storage_key,
                file_data,
                file_size,
                content_type=content_type,
            )
            logger.info(f"Uploaded file to MinIO: {storage_key}")
        except S3Error as e:
            self._raise_storage_error(e, 'upload')

        # Handle image-specific processing
        image_metadata = None
        if content_type in IMAGE_TYPES:
            try:
                image_metadata = self._process_image(
                    file_data, storage_key, content_type, item_id
                )
            except Exception as e:
                logger.warning(f"Failed to process image: {e}")
                # Continue without thumbnail

        return storage_key, file_size, image_metadata

    def _process_image(
        self,
        file_data: BinaryIO,
        storage_key: str,
        content_type: str,
        item_id: int,
    ) -> dict:
        """
        Process an image: get dimensions and generate thumbnail.

        Returns:
            dict with width, height, thumbnail_key
        """
        try:
            from PIL import Image
        except ImportError:
            logger.warning("Pillow not installed, skipping image processing")
            return {'width': None, 'height': None, 'thumbnail_key': ''}

        file_data.seek(0)
        img = Image.open(file_data)
        width, height = img.size

        # Generate thumbnail
        thumbnail_key = ''
        try:
            thumb = img.copy()
            thumb.thumbnail(THUMBNAIL_MAX_SIZE, Image.Resampling.LANCZOS)

            # Save thumbnail to buffer
            thumb_buffer = io.BytesIO()
            thumb_format = 'JPEG' if content_type == 'image/jpeg' else 'PNG'
            thumb.save(thumb_buffer, format=thumb_format, quality=85)
            # Get size by seeking to end, then rewind for upload
            thumb_buffer.seek(0, 2)  # Seek to end
            thumb_size = thumb_buffer.tell()  # Get size
            thumb_buffer.seek(0)  # Rewind for upload

            # Build thumbnail key
            base_key = os.path.splitext(storage_key)[0]
            thumb_ext = '.jpg' if thumb_format == 'JPEG' else '.png'
            temp_thumbnail_key = f"{THUMBNAIL_PREFIX}{base_key}{thumb_ext}"

            thumb_content_type = 'image/jpeg' if thumb_format == 'JPEG' else 'image/png'
            self.client.put_object(
                self.bucket,
                temp_thumbnail_key,
                thumb_buffer,
                thumb_size,
                content_type=thumb_content_type,
            )
            # Only set thumbnail_key after successful upload
            thumbnail_key = temp_thumbnail_key
            logger.info(f"Generated thumbnail: {thumbnail_key}")
        except Exception as e:
            logger.warning(f"Failed to generate thumbnail: {e}")
            # thumbnail_key stays empty string

        return {
            'width': width,
            'height': height,
            'thumbnail_key': thumbnail_key,
        }

    def get_download_url(self, storage_key: str, expires_hours: int = 1) -> str:
        """
        Get a presigned URL for downloading a file.

        Args:
            storage_key: The MinIO object key
            expires_hours: URL validity in hours (default 1)

        Returns:
            Presigned download URL
        """
        try:
            url = self.presign_client.presigned_get_object(
                self.bucket,
                storage_key,
                expires=timedelta(hours=expires_hours),
            )
            return url
        except S3Error as e:
            logger.error(f"Failed to generate download URL: {e}")
            raise

    def get_thumbnail_url(self, thumbnail_key: str, expires_hours: int = 24) -> Optional[str]:
        """
        Get a presigned URL for a thumbnail.

        Args:
            thumbnail_key: The MinIO object key for the thumbnail
            expires_hours: URL validity in hours (default 24)

        Returns:
            Presigned URL or None if no thumbnail
        """
        if not thumbnail_key:
            return None

        try:
            url = self.presign_client.presigned_get_object(
                self.bucket,
                thumbnail_key,
                expires=timedelta(hours=expires_hours),
            )
            return url
        except S3Error as e:
            logger.warning(f"Failed to get thumbnail URL: {e}")
            return None

    def delete_file(self, storage_key: str, thumbnail_key: str = '') -> bool:
        """
        Delete a file and its thumbnail from MinIO.

        Args:
            storage_key: The MinIO object key
            thumbnail_key: Optional thumbnail key

        Returns:
            True if deletion succeeded
        """
        try:
            self.client.remove_object(self.bucket, storage_key)
            logger.info(f"Deleted file from MinIO: {storage_key}")

            if thumbnail_key:
                try:
                    self.client.remove_object(self.bucket, thumbnail_key)
                    logger.info(f"Deleted thumbnail from MinIO: {thumbnail_key}")
                except S3Error:
                    pass  # Thumbnail might not exist

            return True
        except S3Error as e:
            logger.error(f"Failed to delete file: {e}")
            return False

    def file_exists(self, storage_key: str) -> bool:
        """Check if a file exists in MinIO."""
        try:
            self.client.stat_object(self.bucket, storage_key)
            return True
        except S3Error:
            return False


# Singleton instance
attachment_storage = AttachmentStorage()
