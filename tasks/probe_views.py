"""Private workload-identity authenticated readiness probe (PLAT-4601).

This module deliberately does not use Pulse's human JWT authentication.  The
only accepted credential is a short-lived, Pod-bound Kubernetes ServiceAccount
token for the exact production probe identity.  Kubernetes TokenReview is the
authoritative validity and audience check; the local claim parse is used only
to reject structurally unsafe or unexpectedly long-lived tokens before making
an API-server request.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, OrderedDict, deque

from django.db import connection, transaction
from django.http import JsonResponse


EXPECTED_AUDIENCE = "shizuha-pulse-probe"
EXPECTED_USERNAME = "system:serviceaccount:monitoring:prod-ui-probe"
EXPECTED_NAMESPACE = "monitoring"
EXPECTED_SERVICE_ACCOUNT = "prod-ui-probe"
RUNTIME_EVIDENCE_AUDIENCE = "shizuha-pulse-runtime-evidence"
RUNTIME_EVIDENCE_USERNAME = "system:serviceaccount:rt-fleet:shizuha-runtime-fleet"
RUNTIME_EVIDENCE_NAMESPACE = "rt-fleet"
RUNTIME_EVIDENCE_SERVICE_ACCOUNT = "shizuha-runtime-fleet"

MAX_AUTHORIZATION_BYTES = 8192
MAX_TOKEN_LIFETIME_SECONDS = int(os.environ.get("PULSE_PROBE_MAX_TOKEN_TTL_SECONDS", "660"))
TOKEN_REVIEW_TIMEOUT_SECONDS = float(os.environ.get("PULSE_PROBE_TOKEN_REVIEW_TIMEOUT_SECONDS", "2"))
DB_TIMEOUT_MILLISECONDS = int(os.environ.get("PULSE_PROBE_DB_TIMEOUT_MILLISECONDS", "1500"))
RATE_LIMIT_REQUESTS = int(os.environ.get("PULSE_PROBE_RATE_LIMIT_REQUESTS", "30"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("PULSE_PROBE_RATE_LIMIT_WINDOW_SECONDS", "60"))
MAX_CONCURRENT_REQUESTS = int(os.environ.get("PULSE_PROBE_MAX_CONCURRENCY", "4"))
MAX_RATE_LIMIT_SOURCES = int(os.environ.get("PULSE_PROBE_MAX_RATE_LIMIT_SOURCES", "64"))

KUBERNETES_API = os.environ.get("KUBERNETES_SERVICE_URL", "https://kubernetes.default.svc")
TOKEN_REVIEW_URL = KUBERNETES_API.rstrip("/") + "/apis/authentication.k8s.io/v1/tokenreviews"
VERIFIER_TOKEN_PATH = os.environ.get(
    "PULSE_PROBE_VERIFIER_TOKEN_PATH",
    "/var/run/secrets/kubernetes.io/serviceaccount/token",
)
KUBERNETES_CA_PATH = os.environ.get(
    "PULSE_PROBE_KUBERNETES_CA_PATH",
    "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
)

_concurrency = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
_rate_lock = threading.Lock()
# Pre-TokenReview requests are not yet an authenticated workload identity.
# Keep a bounded, source-isolated LRU so one unauthenticated source cannot starve
# the legitimate probe, while old CronJob Pod IP buckets are clock-evicted.
_requests_by_source: OrderedDict[str, deque[float]] = OrderedDict()
_metrics_lock = threading.Lock()
_metrics: Counter[str] = Counter()
_metrics_path = os.environ.get("PULSE_PROBE_METRICS_PATH", "/tmp/pulse-probe-metrics.json")
_metrics_lock_path = _metrics_path + ".lock"


class InvalidProbeToken(Exception):
    """The bearer is malformed, expired, overlong, or not authenticated."""


class ForbiddenProbeIdentity(Exception):
    """The bearer is valid but is not the exact approved probe identity."""


class ProbeAuthUnavailable(Exception):
    """The authoritative TokenReview service could not be reached or trusted."""


def _count(result: str) -> None:
    # Only call with fixed, source-controlled result values.  Never put request
    # data, token material, identities, or exception strings in metric labels.
    with _metrics_lock:
        try:
            with open(_metrics_lock_path, "a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    with open(_metrics_path, encoding="utf-8") as metrics_file:
                        snapshot = json.load(metrics_file)
                except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
                    snapshot = {}
                if not isinstance(snapshot, dict):
                    snapshot = {}
                snapshot[result] = int(snapshot.get(result, 0)) + 1
                temporary_path = f"{_metrics_path}.{os.getpid()}.{threading.get_ident()}"
                with open(temporary_path, "w", encoding="utf-8") as metrics_file:
                    json.dump(snapshot, metrics_file, sort_keys=True, separators=(",", ":"))
                os.replace(temporary_path, _metrics_path)
        except (OSError, TypeError, ValueError):
            # Telemetry can degrade to process-local counters, but it must never
            # change probe authorization/readiness behavior.
            _metrics[result] += 1


def render_probe_auth_metrics() -> str:
    """Render bounded-cardinality Prometheus counters for the probe boundary."""
    with _metrics_lock:
        try:
            with open(_metrics_lock_path, "a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
                with open(_metrics_path, encoding="utf-8") as metrics_file:
                    snapshot = json.load(metrics_file)
            if not isinstance(snapshot, dict):
                snapshot = {}
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            snapshot = dict(_metrics)
    lines = [
        "# HELP pulse_probe_requests_total Private DB-readiness probe requests by sanitized result.",
        "# TYPE pulse_probe_requests_total counter",
    ]
    for result in sorted(snapshot):
        lines.append(
            f'pulse_probe_requests_total{{result="{result}",source="prod-ui-probe"}} '
            f'{snapshot[result]}'
        )
    return "\n".join(lines) + "\n"


def _error(error: str, status: int) -> JsonResponse:
    return JsonResponse({"status": "error", "error": error}, status=status)


def process_liveness_probe(request):
    """Cheap kubelet liveness signal on the isolated probe listener.

    This deliberately does not touch Pulse's database, Redis, MCP transport, or
    main Gunicorn worker pool. Readiness owns those dependencies. If the public
    sync workers are saturated, kubelet must remove the pod from Service
    endpoints without killing the whole supervisord container (and its Celery,
    scheduler, MCP, and probe-listener processes).
    """
    if request.method != "GET":
        response = _error("method_not_allowed", 405)
        response["Allow"] = "GET"
        return response
    return JsonResponse({"status": "ok", "listener": "probe"})


def _decode_unverified_claims(token: str) -> dict:
    """Parse bounded JWT claims without treating them as authorization proof."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        raise InvalidProbeToken
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        if len(raw) > 4096:
            raise InvalidProbeToken
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise InvalidProbeToken
    if not isinstance(claims, dict):
        raise InvalidProbeToken
    return claims


def _validate_bounded_pod_claims(
    token: str, *, now: float | None = None,
    expected_namespace: str = EXPECTED_NAMESPACE,
    expected_service_account: str = EXPECTED_SERVICE_ACCOUNT,
) -> None:
    """Reject unsafe tokens before TokenReview; never authorize from local aud."""
    claims = _decode_unverified_claims(token)
    now = time.time() if now is None else now
    issued_at = claims.get("iat")
    expires_at = claims.get("exp")
    if (
        not isinstance(issued_at, (int, float))
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, (int, float))
        or isinstance(expires_at, bool)
        or expires_at <= now
        or issued_at > now + 30
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_TOKEN_LIFETIME_SECONDS
    ):
        raise InvalidProbeToken

    kubernetes_claims = claims.get("kubernetes.io")
    if not isinstance(kubernetes_claims, dict):
        raise InvalidProbeToken
    service_account = kubernetes_claims.get("serviceaccount")
    pod = kubernetes_claims.get("pod")
    if (
        kubernetes_claims.get("namespace") != expected_namespace
        or not isinstance(service_account, dict)
        or service_account.get("name") != expected_service_account
        or not isinstance(service_account.get("uid"), str)
        or not service_account.get("uid")
        or not isinstance(pod, dict)
        or not isinstance(pod.get("name"), str)
        or not pod.get("name")
        or not isinstance(pod.get("uid"), str)
        or not pod.get("uid")
    ):
        raise InvalidProbeToken


def _read_bounded(path: str, maximum: int) -> str:
    with open(path, "rb") as handle:
        value = handle.read(maximum + 1)
    if not value or len(value) > maximum:
        raise ProbeAuthUnavailable
    try:
        return value.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ProbeAuthUnavailable


def _token_review(token: str, *, audience: str = EXPECTED_AUDIENCE) -> dict:
    """Ask kube-apiserver to authenticate the opaque caller token."""
    try:
        verifier_token = _read_bounded(VERIFIER_TOKEN_PATH, 16384)
        body = json.dumps(
            {
                "apiVersion": "authentication.k8s.io/v1",
                "kind": "TokenReview",
                "spec": {"token": token, "audiences": [audience]},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            TOKEN_REVIEW_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {verifier_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "shizuha-pulse-probe-verifier/1",
            },
        )
        context = ssl.create_default_context(cafile=KUBERNETES_CA_PATH)
        with urllib.request.urlopen(request, timeout=TOKEN_REVIEW_TIMEOUT_SECONDS, context=context) as response:
            if response.status not in (200, 201):
                raise ProbeAuthUnavailable
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ProbeAuthUnavailable
        result = json.loads(raw.decode("utf-8"))
        status = result.get("status")
        if not isinstance(status, dict):
            raise ProbeAuthUnavailable
        return status
    except ProbeAuthUnavailable:
        raise
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError):
        # Deliberately omit exception strings: urllib errors may echo request
        # details and no token/request material may reach logs or responses.
        raise ProbeAuthUnavailable


def _authenticate_workload(
    token: str, *, audience: str, username: str, namespace: str,
    service_account: str,
) -> None:
    _validate_bounded_pod_claims(
        token,
        expected_namespace=namespace,
        expected_service_account=service_account,
    )
    status = (
        _token_review(token)
        if audience == EXPECTED_AUDIENCE
        else _token_review(token, audience=audience)
    )
    if status.get("authenticated") is not True:
        raise InvalidProbeToken
    user = status.get("user")
    if not isinstance(user, dict) or user.get("username") != username:
        raise ForbiddenProbeIdentity
    audiences = status.get("audiences")
    if not isinstance(audiences, list) or audience not in audiences:
        raise ForbiddenProbeIdentity


def _authenticate(token: str) -> None:
    _authenticate_workload(
        token,
        audience=EXPECTED_AUDIENCE,
        username=EXPECTED_USERNAME,
        namespace=EXPECTED_NAMESPACE,
        service_account=EXPECTED_SERVICE_ACCOUNT,
    )


def _source_is_rate_limited(source: str, *, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    source_key = source if isinstance(source, str) and source else "unknown"
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    with _rate_lock:
        stale_sources = []
        for existing_source, window in _requests_by_source.items():
            while window and window[0] <= cutoff:
                window.popleft()
            if not window:
                stale_sources.append(existing_source)
        for existing_source in stale_sources:
            _requests_by_source.pop(existing_source, None)

        window = _requests_by_source.get(source_key)
        if window is None:
            window = deque()
            _requests_by_source[source_key] = window
        else:
            _requests_by_source.move_to_end(source_key)

        if len(window) >= RATE_LIMIT_REQUESTS:
            return True
        window.append(now)

        while len(_requests_by_source) > MAX_RATE_LIMIT_SOURCES:
            _requests_by_source.popitem(last=False)
        return False


def _extract_bearer(request) -> str:
    if request.method != "GET":
        raise ValueError("method")
    if request.META.get("HTTP_TRANSFER_ENCODING"):
        raise ValueError("body")
    content_length = request.META.get("CONTENT_LENGTH", "")
    try:
        if content_length and int(content_length) != 0:
            raise ValueError("body")
    except (TypeError, ValueError):
        raise ValueError("body")

    authorization = request.META.get("HTTP_AUTHORIZATION", "")
    if not isinstance(authorization, str) or len(authorization.encode("utf-8")) > MAX_AUTHORIZATION_BYTES:
        raise ValueError("header")
    if not authorization.startswith("Bearer ") or authorization.count(" ") != 1:
        raise InvalidProbeToken
    token = authorization[7:]
    if not token or "," in token or "\r" in token or "\n" in token:
        raise InvalidProbeToken
    return token


def _bounded_db_read() -> None:
    timeout_ms = max(100, min(DB_TIMEOUT_MILLISECONDS, 5000))
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL statement_timeout = '{timeout_ms}ms'")
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
    if row != (1,):
        raise RuntimeError("database readiness query returned unexpected result")


def db_readiness_probe(request):
    """Exact private GET canary: workload identity plus one bounded DB read."""
    try:
        token = _extract_bearer(request)
    except ValueError as exc:
        reason = str(exc)
        if reason == "method":
            _count("method_rejected")
            response = _error("method_not_allowed", 405)
            response["Allow"] = "GET"
            return response
        if reason == "header":
            _count("header_rejected")
            return _error("authorization_header_too_large", 431)
        _count("body_rejected")
        return _error("request_body_not_allowed", 400)
    except InvalidProbeToken:
        _count("authentication_rejected")
        return _error("invalid_probe_token", 401)

    source = request.META.get("REMOTE_ADDR") or "unknown"
    if _source_is_rate_limited(source):
        _count("rate_limited")
        return _error("probe_rate_limited", 429)
    if not _concurrency.acquire(blocking=False):
        _count("concurrency_limited")
        return _error("probe_busy", 503)
    try:
        try:
            _authenticate(token)
        except InvalidProbeToken:
            _count("authentication_rejected")
            return _error("invalid_probe_token", 401)
        except ForbiddenProbeIdentity:
            _count("identity_rejected")
            return _error("probe_identity_forbidden", 403)
        except ProbeAuthUnavailable:
            _count("auth_unavailable")
            return _error("probe_auth_unavailable", 503)

        try:
            _bounded_db_read()
        except Exception:
            # Never include driver/SQL exception text in the response.  This is
            # a readiness signal, not a database diagnostics endpoint.
            _count("database_unavailable")
            return _error("database_unavailable", 503)

        _count("ok")
        return JsonResponse({"status": "ok", "database": "ok", "auth": "workload_identity"})
    finally:
        _concurrency.release()


def runtime_live_probe_receipt(request):
    """Mint a typed receipt from the exact runtime-controller workload identity.

    The route is exposed only on the ClusterIP probe listener. TokenReview binds
    the request to a short-lived Pod token for rt-fleet/shizuha-runtime-fleet;
    the body may select only a task + already-correlated successful DeploymentEvent.
    Pulse re-runs all correlation, image, and convergence checks server-side.
    """
    if request.method != "POST":
        response = _error("method_not_allowed", 405)
        response["Allow"] = "POST"
        return response
    if request.META.get("HTTP_TRANSFER_ENCODING"):
        return _error("request_body_invalid", 400)
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        return _error("request_body_invalid", 400)
    if content_length <= 0 or content_length > 16384:
        return _error("request_body_invalid", 400)
    authorization = request.META.get("HTTP_AUTHORIZATION", "")
    if (
        not isinstance(authorization, str)
        or len(authorization.encode("utf-8")) > MAX_AUTHORIZATION_BYTES
        or not authorization.startswith("Bearer ")
        or authorization.count(" ") != 1
    ):
        return _error("invalid_probe_token", 401)
    token = authorization[7:]
    if not token or "," in token or "\r" in token or "\n" in token:
        return _error("invalid_probe_token", 401)
    try:
        _authenticate_workload(
            token,
            audience=RUNTIME_EVIDENCE_AUDIENCE,
            username=RUNTIME_EVIDENCE_USERNAME,
            namespace=RUNTIME_EVIDENCE_NAMESPACE,
            service_account=RUNTIME_EVIDENCE_SERVICE_ACCOUNT,
        )
    except InvalidProbeToken:
        return _error("invalid_probe_token", 401)
    except ForbiddenProbeIdentity:
        return _error("probe_identity_forbidden", 403)
    except ProbeAuthUnavailable:
        return _error("probe_auth_unavailable", 503)

    try:
        body = request.body
        if len(body) > 16384:
            raise ValueError
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        allowed = {"item_key", "deployment_event_id", "evidence"}
        if set(payload) - allowed:
            raise ValueError
        item_key = str(payload.get("item_key") or "").strip().upper()
        deployment_event_id = str(payload.get("deployment_event_id") or "").strip()
        evidence = str(payload.get("evidence") or "").strip()
        if (
            not item_key or len(item_key) > 32
            or not deployment_event_id or len(deployment_event_id) > 64
            or not evidence or len(evidence) > 4000
        ):
            raise ValueError
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _error("request_body_invalid", 400)

    from .models import DeploymentEvent, Item
    from .services.workflow_service import WorkflowService, WorkflowValidationError
    try:
        item = Item.objects.get(item_key=item_key)
        deployment_event = DeploymentEvent.objects.get(pk=deployment_event_id)
    except (Item.DoesNotExist, DeploymentEvent.DoesNotExist, ValueError):
        return _error("correlated_deployment_not_found", 404)
    try:
        comment = WorkflowService.issue_runtime_live_probe_receipt(
            item,
            deployment_event=deployment_event,
            evidence=evidence,
            workload_identity=RUNTIME_EVIDENCE_USERNAME,
        )
    except WorkflowValidationError as exc:
        return JsonResponse({"status": "error", "error": str(exc)}, status=403)
    return JsonResponse({
        "status": "ok",
        "receipt_id": comment.id,
        "origin_event": comment.origin_event,
        "origin_event_id": comment.origin_event_id,
        "origin_payload": comment.origin_payload,
    }, status=201)


def _reset_probe_state_for_tests() -> None:
    """Reset process-local limiter/counters; only imported by the test suite."""
    with _rate_lock:
        _requests_by_source.clear()
    with _metrics_lock:
        _metrics.clear()
        for path in (_metrics_path, _metrics_lock_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
