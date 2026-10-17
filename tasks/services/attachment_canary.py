"""Authenticated, bounded Pulse attachment-storage canary (PLS-483).

The canary deliberately uses the production ``AttachmentStorage`` client while
keeping its objects in a reserved prefix.  It accepts no caller payload, bucket,
endpoint, or object key.  Every operation is exact-key and every listing is
prefix-scoped, so customer attachment data is never enumerated or returned.
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import urllib3
from minio.error import S3Error

from .storage import (
    AttachmentStorage,
    AttachmentStorageAuthenticationError,
    AttachmentStorageConfigurationError,
    attachment_storage,
)


CANARY_PREFIX = "_canary/pulse-attachment/"
CANARY_CONTENT_TYPE = "application/octet-stream"
CANARY_PAYLOAD = b"pls-483:pulse-attachment-storage-canary:v1\n"
CANARY_TTL = timedelta(hours=1)
CANARY_CLEANUP_LIMIT = 100
# Independent deployment trust anchors: never default to MINIO_* runtime
# values, which would make the drift detector compare a setting with itself.
# A topology change requires a reviewed update to these separate expectations.
EXPECTED_MINIO_ENDPOINT = os.environ.get('PULSE_EXPECTED_MINIO_ENDPOINT', 'localhost:9000')
EXPECTED_MINIO_BUCKET = os.environ.get('PULSE_EXPECTED_MINIO_BUCKET', 'pulse-attachments')
EXPECTED_MINIO_REGION = os.environ.get('PULSE_EXPECTED_MINIO_REGION', 'us-east-1')
# PLS-483 (review P1 #1): bound the canary's own MinIO operations so a timed-out
# run aborts well within the run budget instead of the SDK default 300s+retries;
# the run-lock TTL then genuinely covers the op's max lifetime (no overlap).
CANARY_CONNECT_TIMEOUT_SECONDS = 5.0
CANARY_READ_TIMEOUT_SECONDS = 8.0
# PLS-483 (review P1 #3): `topology` verifies endpoint, bucket and live bucket
# region against the independent immutable baseline; the bucket must also exist.
# The canary client never auto-creates, so drift cannot self-heal before proof.
PHASES = ("auth", "topology", "cleanup", "create", "metadata", "read", "validate", "delete")


@dataclass
class AttachmentCanaryFailure(RuntimeError):
    phase: str
    reason: str
    phases: Dict[str, bool]

    def __str__(self) -> str:
        return f"attachment canary failed at {self.phase}: {self.reason}"


def _reason(error: Exception) -> str:
    if isinstance(error, AttachmentStorageConfigurationError):
        return "configuration"
    if isinstance(error, AttachmentStorageAuthenticationError):
        return "signature_mismatch"
    if isinstance(error, S3Error):
        code = str(getattr(error, "code", "") or "")
        return {
            "SignatureDoesNotMatch": "signature_mismatch",
            "InvalidAccessKeyId": "invalid_access_key",
            "AccessDenied": "access_denied",
            "NoSuchBucket": "bucket_missing",
            "InvalidRegion": "region_mismatch",
            "AuthorizationHeaderMalformed": "region_mismatch",
        }.get(code, "storage_error")
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ValueError):
        return {
            "size mismatch": "size_mismatch",
            "content type mismatch": "content_type_mismatch",
            "checksum metadata mismatch": "metadata_mismatch",
        }.get(str(error), "validation_failed")
    if isinstance(error, RuntimeError):
        return "cleanup_failed"
    return "unexpected"


def _metadata_value(metadata, name: str) -> str:
    wanted = {name.lower(), f"x-amz-meta-{name.lower()}"}
    for key, value in (metadata or {}).items():
        if str(key).lower() in wanted:
            return str(value)
    return ""


def _cleanup_stale(storage: AttachmentStorage, now: datetime) -> int:
    """Delete only expired objects from the reserved canary prefix."""
    removed = 0
    seen = 0
    for obj in storage.client.list_objects(
        storage.bucket,
        prefix=CANARY_PREFIX,
        recursive=True,
    ):
        seen += 1
        if seen > CANARY_CLEANUP_LIMIT:
            raise RuntimeError("canary cleanup limit exceeded")
        modified = getattr(obj, "last_modified", None)
        if modified is None:
            continue
        if modified.tzinfo is None:
            modified = modified.replace(tzinfo=timezone.utc)
        if modified <= now - CANARY_TTL:
            storage.client.remove_object(storage.bucket, obj.object_name)
            removed += 1
    return removed


def _canary_http_client() -> urllib3.PoolManager:
    """A urllib3 client with BOUNDED timeouts and no retries for the canary.

    The shared production MinIO client keeps the SDK defaults (300s + retries);
    the canary must not, so a hung/slow storage op aborts within the run budget
    and cannot outlive the singleton run-lock TTL (review P1 #1)."""
    return urllib3.PoolManager(
        timeout=urllib3.Timeout(
            connect=CANARY_CONNECT_TIMEOUT_SECONDS,
            read=CANARY_READ_TIMEOUT_SECONDS,
        ),
        retries=urllib3.Retry(total=0, connect=0, read=0, redirect=0),
        maxsize=2,
    )


def _canary_storage(fault_mode: str) -> AttachmentStorage:
    """Build the canary's ISOLATED storage client: bounded timeouts + no
    auto-create (a drift to a missing bucket must FAIL, not self-heal).
    fault_mode='signature' injects a wrong secret to reproduce PLS-413."""
    secret_key = (
        "pls483-intentional-invalid-secret" if fault_mode == "signature"
        else attachment_storage.secret_key
    )
    return AttachmentStorage(
        endpoint=attachment_storage.endpoint,
        access_key=attachment_storage.access_key,
        secret_key=secret_key,
        bucket=attachment_storage.bucket,
        secure=attachment_storage.secure,
        http_client=_canary_http_client(),
        ensure_bucket=False,
    )


def _verify_topology(storage: AttachmentStorage) -> None:
    """Validate live config against an independent, immutable topology.

    The comparison must not use ``MINIO_*`` runtime values: those same values
    construct ``attachment_storage`` and would make the detector tautological.
    The check is read-only and completes before the canary writes an object.
    """
    if storage.endpoint != EXPECTED_MINIO_ENDPOINT:
        raise AttachmentCanaryFailure("topology", "endpoint_mismatch", {})
    if storage.bucket != EXPECTED_MINIO_BUCKET:
        raise AttachmentCanaryFailure("topology", "bucket_mismatch", {})
    # Resolve the region before any other MinIO request: minio-py's request
    # path also consumes its private resolver, so validating our adapter first
    # is what keeps an SDK call-shape drift typed as configuration rather than
    # leaking out of bucket_exists() as an opaque unexpected failure.
    region = storage.get_bucket_region()
    if region != EXPECTED_MINIO_REGION:
        raise AttachmentCanaryFailure("topology", "region_mismatch", {})
    if not storage.client.bucket_exists(storage.bucket):
        raise AttachmentCanaryFailure("topology", "bucket_missing", {})


def run_attachment_storage_canary(
    *,
    fault_mode: str = "",
    storage: Optional[AttachmentStorage] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Create, stat, read, validate, delete, and prove exact cleanup."""
    if fault_mode not in ("", "signature"):
        raise ValueError("unsupported fault mode")
    storage = storage or _canary_storage(fault_mode)
    now = now or datetime.now(timezone.utc)
    phases = {phase: False for phase in PHASES}
    key = f"{CANARY_PREFIX}{uuid.uuid4().hex}.bin"
    digest = hashlib.sha256(CANARY_PAYLOAD).hexdigest()
    created = False

    try:
        try:
            # Lazy client construction performs the real bucket/auth/endpoint
            # check used by normal attachments.
            client = storage.client
            phases["auth"] = True
        except Exception as error:
            raise AttachmentCanaryFailure("auth", _reason(error), phases.copy()) from error

        try:
            # PLS-483 P1 #3: non-mutating drift check BEFORE any create — the
            # endpoint/bucket/region must match the immutable production baseline
            # and the bucket must already EXIST (no auto-create).
            _verify_topology(storage)
            phases["topology"] = True
        except AttachmentCanaryFailure as failure:
            raise AttachmentCanaryFailure("topology", failure.reason, phases.copy()) from failure
        except Exception as error:
            raise AttachmentCanaryFailure("topology", _reason(error), phases.copy()) from error

        try:
            removed = _cleanup_stale(storage, now)
            phases["cleanup"] = True
        except Exception as error:
            raise AttachmentCanaryFailure("cleanup", _reason(error), phases.copy()) from error

        try:
            client.put_object(
                storage.bucket,
                key,
                io.BytesIO(CANARY_PAYLOAD),
                len(CANARY_PAYLOAD),
                content_type=CANARY_CONTENT_TYPE,
                metadata={"sha256": digest, "canary": "pls-483"},
            )
            created = True
            phases["create"] = True
        except Exception as error:
            raise AttachmentCanaryFailure("create", _reason(error), phases.copy()) from error

        try:
            stat = client.stat_object(storage.bucket, key)
            if int(getattr(stat, "size", -1)) != len(CANARY_PAYLOAD):
                raise ValueError("size mismatch")
            if str(getattr(stat, "content_type", "")) != CANARY_CONTENT_TYPE:
                raise ValueError("content type mismatch")
            if _metadata_value(getattr(stat, "metadata", {}), "sha256") != digest:
                raise ValueError("checksum metadata mismatch")
            phases["metadata"] = True
        except Exception as error:
            raise AttachmentCanaryFailure("metadata", _reason(error), phases.copy()) from error

        response = None
        try:
            response = client.get_object(storage.bucket, key)
            # One byte beyond the expected fixed payload detects oversize reads
            # without ever buffering an unbounded response.
            payload = response.read(len(CANARY_PAYLOAD) + 1)
            phases["read"] = True
        except Exception as error:
            raise AttachmentCanaryFailure("read", _reason(error), phases.copy()) from error
        finally:
            if response is not None:
                response.close()
                response.release_conn()

        if payload != CANARY_PAYLOAD or hashlib.sha256(payload).hexdigest() != digest:
            raise AttachmentCanaryFailure("validate", "content_mismatch", phases.copy())
        phases["validate"] = True

        try:
            client.remove_object(storage.bucket, key)
            try:
                client.stat_object(storage.bucket, key)
            except S3Error as error:
                if str(getattr(error, "code", "")) not in ("NoSuchKey", "NoSuchObject"):
                    raise
            else:
                raise RuntimeError("deleted canary object still exists")
            created = False
            phases["delete"] = True
        except Exception as error:
            raise AttachmentCanaryFailure("delete", _reason(error), phases.copy()) from error

        return {
            "ok": True,
            "phases": phases,
            "stale_objects_removed": removed,
            "payload_size": len(CANARY_PAYLOAD),
        }
    finally:
        if created:
            try:
                storage.client.remove_object(storage.bucket, key)
            except Exception:
                # The next run's TTL sweep is the independent fallback. Never
                # log the exact key or credentials here.
                pass
