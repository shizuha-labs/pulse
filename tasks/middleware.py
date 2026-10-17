"""
Middleware for request-local storage and service access control.

Used to access the current request in signals and other contexts
where the request is not directly available.
"""

import logging
import threading

from django.db import OperationalError
from django.http import JsonResponse

logger = logging.getLogger(__name__)

# Thread-local storage for request
_request_local = threading.local()


class RequestLocalMiddleware:
    """
    Middleware that stores the current request in thread-local storage.
    This allows signals to access the current user without passing it explicitly.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _request_local.request = request
        try:
            response = self.get_response(request)
            self._schedule_agent_failback(request, response)
        finally:
            # Clean up to prevent memory leaks
            if hasattr(_request_local, 'request'):
                del _request_local.request
        return response

    @staticmethod
    def _schedule_agent_failback(request, response):
        """Turn a successful authenticated request into a scoped recovery event.

        Authentication writes the liveness timestamp before the view mutates
        state.  Scheduling only after a successful response is essential: a
        bridge's ``active=false`` request must first commit its newer durable
        unavailability row, otherwise its own pre-write heartbeat could race a
        failback and resurrect work while the bridge is shutting down.
        """
        if getattr(response, 'status_code', 500) >= 400:
            return
        # The self-availability endpoint already dispatches the ordered
        # health-refresh -> failback -> queue-drain task from transaction.on_commit.
        # Skipping it here avoids a duplicate task on active=true and, more
        # importantly, prevents an active=false shutdown report from consuming
        # an older pending recovery hint.
        if str(getattr(request, 'path', '') or '').rstrip('/').endswith(
            '/agent-availability/self'
        ):
            return
        user = getattr(request, 'user', None)
        if not user or not getattr(user, 'is_authenticated', False):
            return
        email = (getattr(user, 'email', '') or '').strip().lower()
        if not email:
            return
        try:
            from .agent_liveness import claim_agent_failback_probe

            if not claim_agent_failback_probe(email):
                return
            from .tasks import restore_false_positive_failovers_for_agent

            # A tiny delay lets the request transaction and its task-local
            # activity audit commit before the row-locked CAS check runs.
            restore_false_positive_failovers_for_agent.apply_async(
                kwargs={'only_agent_email': email},
                countdown=2,
            )
        except Exception:
            # Liveness/failback observability must never change API behavior.
            logger.exception(
                'PLAT-5032 failback event dispatch failed for %s; a later '
                'authenticated request may retry',
                email,
            )


def get_current_request():
    """
    Get the current request from thread-local storage.
    Returns None if not in a request context.
    """
    return getattr(_request_local, 'request', None)


def get_current_user():
    """
    Get the current user from the request.
    Returns None if not authenticated or not in a request context.
    """
    request = get_current_request()
    if request and hasattr(request, 'user') and request.user.is_authenticated:
        return request.user
    return None


API_PATH_PREFIXES = ('/api/', '/shizuha-pulse/api/', '/pulse/api/')

_INTERNAL_PATH_MARKERS = ('/api/internal/', '/shizuha-pulse/api/internal/', '/pulse/api/internal/')


class InternalApiIngressGuardMiddleware:
    """Block ingress-proxied requests to internal service-to-service endpoints.

    /api/internal/* is meant for in-cluster pod-to-pod calls only. The public
    nginx ingress always appends X-Forwarded-For / X-Real-IP to proxied
    requests; direct in-cluster Service calls carry neither. We detect and
    reject ingress-borne traffic with 404 — don't advertise the path exists.

    Same pattern as shizuha-id InternalApiIngressGuardMiddleware (PLS-383).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if any(marker in path for marker in _INTERNAL_PATH_MARKERS):
            meta = request.META
            if meta.get('HTTP_X_FORWARDED_FOR') or meta.get('HTTP_X_REAL_IP'):
                return JsonResponse({'error': 'Not found'}, status=404)
        return self.get_response(request)


def is_public_api_path(request):
    paths = {getattr(request, 'path', ''), getattr(request, 'path_info', '')}
    return any(path.startswith(API_PATH_PREFIXES) for path in paths if path)


class StructuredApiExceptionMiddleware:
    """Return machine-readable 5xx responses for API clients.

    Agents call Pulse through MCP tools; an HTML 500 page gives them no way to
    classify a transient Pulse outage or choose a sane retry/backoff path.  Keep
    admin/non-API behaviour unchanged, but make API failures explicit.
    """

    RETRY_AFTER_SECONDS = 30

    @staticmethod
    def _exception_chain_texts(exception):
        """Collect str() of the exception and up to four cause levels (PLAT-7422).

        Django wraps the psycopg error in ``IntegrityError`` with the driver
        error as ``__cause__``; some wrappers carry a generic outer message, so
        branch detection and item-id parsing must both scan the chain.
        """
        texts = [str(exception)]
        seen_causes = set()
        cause = exception
        for _ in range(4):
            cause = getattr(cause, '__cause__', None) or getattr(cause, 'origin', None)
            if cause is None or id(cause) in seen_causes:
                break
            seen_causes.add(id(cause))
            texts.append(str(cause))
            diag = getattr(cause, 'diag', None)
            primary = getattr(diag, 'message_primary', None)
            if primary:
                texts.append(str(primary))
        return texts

    @staticmethod
    def _resolve_naked_blocked_items(exception):
        """Extract the naked blocked task(s) the PLAT-4215 trigger named (PLAT-7422).

        The trigger raises ``MESSAGE = 'blocked_item_requires_active_assigned_
        blocker: item_id=<n>'``; parse every ``item_id=`` occurrence from the
        exception chain and resolve each to its item key/status. Resolution is
        best-effort — a failure here must never mask the 409 itself.
        """
        import re as _re

        texts = StructuredApiExceptionMiddleware._exception_chain_texts(exception)
        ids = []
        for text in texts:
            for match in _re.finditer(r'item_id=(\d+)', text or ''):
                item_id = int(match.group(1))
                if item_id not in ids:
                    ids.append(item_id)
        if not ids:
            return []

        try:
            from .models import Item
            rows = Item.objects.filter(id__in=ids).values_list(
                'id', 'item_key', 'status',
            )
            by_id = {row[0]: (row[0], row[1], row[2]) for row in rows}
            return [by_id.get(i, (i, '', '')) for i in ids]
        except Exception:
            return [(i, '', '') for i in ids]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if not is_public_api_path(request):
            return None

        # PLS-705: a PostgreSQL statement_timeout on task search is a
        # deterministic request-cost rejection, not a transient server outage.
        # The generic 500 below tells MCP clients to retry after 30s, which
        # rebuilt the synchronized query herd that caused the cancellation.
        # Match only the known GET /api/items|tasks/?search= QueryCanceled boundary;
        # other OperationalError instances may be genuinely transient and must
        # retain the server-error/backoff contract.
        cause = getattr(exception, '__cause__', None)
        is_statement_timeout = (
            isinstance(exception, OperationalError)
            and cause is not None
            and cause.__class__.__name__ == 'QueryCanceled'
            and 'statement timeout' in f'{cause} {exception}'.lower()
        )
        request_paths = {
            getattr(request, 'path', ''),
            getattr(request, 'path_info', ''),
        }
        is_item_search = (
            getattr(request, 'method', '').upper() == 'GET'
            and any(
                path in {f'{prefix}items/', f'{prefix}tasks/'}
                for path in request_paths
                if path
                for prefix in API_PATH_PREFIXES
            )
            and bool(getattr(request, 'GET', {}).get('search', '').strip())
        )
        if is_statement_timeout and is_item_search:
            logger.warning(
                'Pulse task search rejected at statement_timeout on %s; '
                'returning non-retryable query_cost_exceeded',
                getattr(request, 'path', getattr(request, 'path_info', '')),
            )
            return JsonResponse(
                {
                    'error': 'query_cost_exceeded',
                    'code': 'query_cost_exceeded',
                    'detail': (
                        'Pulse task search exceeded its bounded database execution '
                        'budget. Narrow the query or use a keyed task read; retrying '
                        'the identical search will not succeed.'
                    ),
                    'retryable': False,
                },
                status=422,
            )

        # PLAT-4215: the blocked-task DB constraint raising at write time is a
        # CLIENT-actionable conflict, not a server fault. Surface it as a 409
        # with the trigger's guidance instead of an opaque 500 (agents were
        # retrying assign/link calls into a 500 storm, 2026-07-14).
        #
        # PLAT-7422 Defect 1 (message layer): the trigger's PG message carries
        # the ACTUAL naked blocked item — ``item_id=%s`` — but this handler used
        # to discard it and emit a static detail whose "(PLAT-4215)" token is
        # the POLICY provenance, not a task citation. Five completion refusals
        # (PLAT-7389 4x, PLAT-7406 1x, 2026-09-01/02) were misread as "the
        # blocker is task PLAT-4215" and burned an exhaustive unlink-probe
        # investigation refuting a citation that was never made. Parse the real
        # item id(s), resolve their keys, and label the policy explicitly.
        if any(
            'blocked_item_requires_active_assigned_blocker' in text
            for text in self._exception_chain_texts(exception)
        ):
            naked = self._resolve_naked_blocked_items(exception)
            if naked:
                named = ', '.join(
                    f"[{key or item_id}] (id={item_id}, status={stat or '?'})"
                    for item_id, key, stat in naked
                )
                named_tasks = ', '.join(
                    f"[{key or item_id}]" for item_id, key, _ in naked
                )
            else:
                named = 'the trigger message did not carry a parsable item_id'
                named_tasks = '(unresolved — see server logs for the raw message)'
            return JsonResponse(
                {
                    'error': 'blocked_item_requires_active_assigned_blocker',
                    'blocked_items': [
                        {
                            'item_id': item_id,
                            'item_key': key,
                            'status': stat,
                        }
                        for item_id, key, stat in naked
                    ],
                    'detail': (
                        'The database rejected this change: blocked task(s) '
                        f'{named_tasks} would be left without an owned active '
                        'blocker. NOTE: "PLAT-4215" is the POLICY that mandates '
                        'this invariant — it is NOT the blocker task and NOT the '
                        'naked dependent; the naked task(s) are: '
                        f'{named}. Every blocked task must keep at least one '
                        'active linked blocker that is concretely assigned or '
                        'routed to a team (assignment_group). Unblock the '
                        'dependent task first, or assign/team-route its blocker '
                        'so it stays owned.'
                    ),
                },
                status=409,
            )

        # PLS-375: log the swallowed exception WITH its stacktrace. Returning a
        # response from process_exception suppresses Django's default
        # handle_uncaught_exception logging, so without this every API 5xx —
        # including the recurring transient transition/complete 500s
        # (PLAT-1049 / PLS-167 / PLS-375) — is discarded and the incident cannot
        # be root-caused from the Pulse logs. Log here so the next occurrence is
        # diagnosable (method + path + full traceback).
        logger.error(
            "Unhandled API exception -> server_error 500 on %s %s: %r",
            getattr(request, 'method', '?'),
            getattr(request, 'path', getattr(request, 'path_info', '?')),
            exception,
            exc_info=exception,
        )

        return JsonResponse(
            {
                'error': 'server_error',
                'detail': 'Pulse API encountered an internal error. Retry after the suggested backoff; if it persists, check /api/health/ and Pulse server logs.',
                'retry_after': self.RETRY_AFTER_SECONDS,
            },
            status=500,
            headers={'Retry-After': str(self.RETRY_AFTER_SECONDS)},
        )


class ServiceAccessMiddleware:
    SERVICE_SLUG = 'pulse'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not is_public_api_path(request):
            return self.get_response(request)
        if '/internal/' in request.path:
            return self.get_response(request)

        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
            try:
                # Reuse Pulse's canonical HS256/RS256/JWKS validation path.  A
                # restrictive service list must not be skipped just because a
                # local HS256-only decode cannot parse the token during migration.
                from .authentication import ShizuhaJWTAuthentication

                payload = ShizuhaJWTAuthentication().get_validated_token(token)
            except Exception:
                # Let DRF authentication handle invalid tokens; this middleware
                # enforces service access only after canonical validation succeeds.
                return self.get_response(request)

            if payload.get('is_staff') or payload.get('is_superuser'):
                return self.get_response(request)
            enabled_services = payload.get('enabled_services')
            if enabled_services is not None and self.SERVICE_SLUG not in enabled_services:
                # PLS-253: this identity's runtime is alive (it just made an
                # authenticated request) but its Pulse service grant is revoked.
                # Record it as an availability-negative routing signal + emit an
                # observable warning so the resolver/rebalancer skip and drain the
                # locked-out seat instead of silently piling high-priority work on
                # it (the nova incident, 2026-07-03). Best-effort — never let this
                # observability path change the 403 response.
                self._record_service_denied(payload)
                return JsonResponse(
                    {'error': 'You do not have access to this service.',
                     'service': self.SERVICE_SLUG,
                     'code': 'service_access_denied'},
                    status=403
                )

        return self.get_response(request)

    def _record_service_denied(self, payload):
        try:
            email = (payload.get('email') or '').strip().lower()
            if not email:
                username = (payload.get('username') or '').strip().lower()
                if username:
                    email = f'{username}@shizuha.com'
            if not email:
                return
            from .services.workflow_service import WorkflowService
            WorkflowService.mark_agent_service_denied(email)
            logger.warning(
                'pulse service_access_denied for live agent identity %s — marking '
                'seat unavailable for routing (PLS-253)', email,
            )
        except Exception:
            # Observability must never break the auth/deny path.
            pass
