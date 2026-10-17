"""PLAT-5928 — strict Alertmanager durable webhook endpoint (AC1/AC2).

POST /api/webhooks/alertmanager-durable/<key>/

- Strict-decodes the raw request bytes (§8.4.1B) and validates the WHOLE batch
  before any transaction. Any member failure → HTTP 400 with zero receipt /
  episode / phase / outbox / cache / channel state.
- A 2xx is returned only after one Postgres transaction durably creates/joins
  every AlertIngressReceiptV1, episode, recipient phase and due outbox row.
- Exact replay returns the same acceptance IDs; conflicting content cannot
  overwrite the first body or mint a second effect.
"""
from __future__ import annotations

from django.db import DatabaseError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .acceptance import BadBatchError, accept_batch, validate_batch


class WebhookKeyAdapter:
    """Minimal adapter from the tasks WebhookKey model to the acceptance API."""

    def __init__(self, webhook_key):
        self._wk = webhook_key
        self.id = webhook_key.id
        self.source_name = webhook_key.source_name
        self.default_severity = getattr(webhook_key, 'default_severity', 'warning')
        self.default_project = getattr(webhook_key, 'default_project', None)
        self.webhook_key_revision = getattr(
            webhook_key, 'webhook_key_revision', 1) or 1
        self._effective_organization_id = _org_id_for(webhook_key)

    @property
    def default_assignment_group(self):
        return getattr(self._wk, 'default_assignment_group', None)


def _org_id_for(webhook_key):
    project = getattr(webhook_key, 'default_project', None)
    if project is not None:
        return getattr(project, 'org_id', None)
    return getattr(webhook_key, '_effective_organization_id', None)


class PrometheusAlertmanagerDurableWebhookView(APIView):
    """Durable Alertmanager v2 batch acceptance (producer lineage V1)."""

    permission_classes = []  # auth via webhook key

    def post(self, request, key):
        from tasks.models import WebhookKey

        try:
            wk = WebhookKey.objects.select_related('default_project').get(
                key=key, is_active=True)
        except WebhookKey.DoesNotExist:
            return Response({'error': 'invalid_webhook_key'},
                            status=status.HTTP_401_UNAUTHORIZED)

        raw = request.body
        try:
            decoded = validate_batch(raw, webhook=WebhookKeyAdapter(wk))
        except BadBatchError as exc:
            reason = getattr(exc, 'reason', 'batch_invalid')
            return Response({'error': 'batch_rejected', 'reason': reason},
                            status=status.HTTP_400_BAD_REQUEST)

        recipient_map = _resolve_recipient_map(decoded, webhook_key=wk)

        try:
            report = accept_batch(decoded, recipient_map=recipient_map)
        except DatabaseError:
            return Response({'error': 'commit_failed'},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)

        from .wake import post_commit_wake
        post_commit_wake(report)

        return Response({
            'schema': 'shizuha.alert-acceptance-report.v1',
            'accepted': report['accepted'],
            'joins': report['joins'],
            'conflicts': report['conflicts'],
            'episodes': report['episodes'],
            'phases': report['phases'],
            'outbox': report['outbox'],
            'suppressed_unrouted': report['suppressed_unrouted'],
        }, status=status.HTTP_202_ACCEPTED)


def _resolve_recipient_map(decoded, *, webhook_key):
    """Plan-time exact-org recipient resolution per envelope digest.

    An empty exact-org roster → the envelope is suppressed_unrouted (terminal),
    never an ordinary fallback target.
    """
    from .recipients import resolve_recipients_for_webhook
    mapping = resolve_recipients_for_webhook(webhook_key)
    return {env.envelope_digest: mapping for env in decoded}
