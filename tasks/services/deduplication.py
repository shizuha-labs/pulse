"""
Alert Deduplication Service

Handles fingerprint computation and deduplication logic for alerts.
"""

import logging
import os
from typing import Optional, Tuple
from django.db import transaction
from django.utils import timezone

from tasks.models import Item, FingerprintConfig, Project

logger = logging.getLogger(__name__)


class AlertIdentityError(ValueError):
    """Raised when an alert payload has no stable deduplication identity."""


class AlertDeduplicationService:
    """
    Service for handling alert deduplication.

    Alerts are deduplicated based on their fingerprint, which is computed from:
    - item_type
    - source
    - configured label fields
    - configured payload fields
    """

    # Default fingerprint config (used when no source-specific config exists)
    DEFAULT_CONFIG = {
        'label_fields': None,  # None = use all labels
        'payload_fields': [],
    }

    # ADR-0020 / PLS-293: k3s Alertmanager must dedupe on a bounded,
    # source-specific identity when Alertmanager's own ``fingerprint`` is
    # absent. Without this, every transient exporter/pod label can create a new
    # active Pulse alert row. Keep this list to schema/alert identity labels.
    BUILTIN_SOURCE_CONFIGS = {
        'k3s-alertmanager': {
            'label_fields': [
                'alertname', 'namespace', 'job', 'job_name', 'pod',
                'poddisruptionbudget', 'deployment', 'daemonset', 'statefulset',
                'service', 'endpoint', 'instance', 'node',
                'persistentvolumeclaim', 'container', 'reason', 'condition',
                'cluster',
            ],
            'payload_fields': [],
        },
    }

    @classmethod
    def get_fingerprint_config(cls, source: str) -> dict:
        """
        Get fingerprint configuration for a source.
        Falls back to default if no specific config exists.
        """
        try:
            config = FingerprintConfig.objects.get(source=source)
            return {
                'label_fields': config.label_fields if config.label_fields else None,
                'payload_fields': config.payload_fields or [],
            }
        except FingerprintConfig.DoesNotExist:
            return cls.BUILTIN_SOURCE_CONFIGS.get(source, cls.DEFAULT_CONFIG)

    @classmethod
    def _identity_fields_present(cls, labels: dict, payload: dict, config: dict) -> list[str]:
        """Return the configured fields that contributed identity material."""
        labels = labels or {}
        payload = payload or {}
        present = []
        label_fields = config.get('label_fields')
        if label_fields is None:
            present.extend(f"label:{k}" for k in sorted(labels.keys()))
        else:
            present.extend(f"label:{k}" for k in sorted(label_fields) if k in labels)
        present.extend(
            f"payload:{k}"
            for k in (config.get('payload_fields') or [])
            if k in payload
        )
        if payload.get('correlation_id'):
            present.append('payload:correlation_id')
        return present

    @classmethod
    def validate_identity(cls, data: dict, config: dict) -> None:
        """Fail closed when neither upstream id nor configured fields identify the alert."""
        if data.get('source_id'):
            return
        if data.get('correlation_id'):
            return

        labels = data.get('labels') or {}
        payload = data.get('payload') or {}
        present = cls._identity_fields_present(labels, payload, config)
        if present:
            return

        raise AlertIdentityError(
            f"alert source={data.get('source')!r} title={data.get('title')!r} "
            "has no source_id and no configured label/payload identity fields"
        )

    @classmethod
    def compute_fingerprint(
        cls,
        item_type: str,
        source: str,
        labels: dict,
        payload: dict = None,
        config: dict = None
    ) -> str:
        """
        Compute fingerprint for an alert.
        Delegates to Item.compute_fingerprint for the actual computation.
        """
        if config is None:
            config = cls.get_fingerprint_config(source)

        return Item.compute_fingerprint(
            item_type=item_type,
            source=source,
            labels=labels,
            payload=payload,
            config=config
        )

    @classmethod
    @transaction.atomic
    def submit_alert(cls, data: dict) -> Tuple[Item, bool]:
        """
        Submit an alert with automatic deduplication.

        Args:
            data: Alert data including:
                - item_type (required): e.g., "alert.error"
                - title (required): Alert title
                - source (required): Source identifier
                - labels (optional): Dict of labels
                - payload (optional): Additional payload data
                - severity (optional): info/warning/error/critical
                - description (optional): Alert description
                - due_date (optional): Auto-resolve time
                - correlation_id (optional): For grouping related alerts

        Returns:
            Tuple of (Item, is_new) where is_new is True if a new alert was created
        """
        # Extract required fields
        item_type = data.get('item_type', 'alert.error')
        title = data['title']
        source = data['source']
        labels = data.get('labels', {})
        payload = data.get('payload', {})

        # Ensure mode is alert and item_type has alert prefix
        if not item_type.startswith('alert.'):
            item_type = f'alert.{item_type}'

        # Get fingerprint config and compute fingerprint
        config = cls.get_fingerprint_config(source)
        if data.get('correlation_id'):
            payload = {**payload, 'correlation_id': data['correlation_id']}
            config = {
                **config,
                'payload_fields': sorted(set(config.get('payload_fields') or []) | {'correlation_id'}),
            }
        cls.validate_identity(data, config)
        fingerprint = cls.compute_fingerprint(
            item_type=item_type,
            source=source,
            labels=labels,
            payload=payload,
            config=config
        )

        # Check for an existing active alert to deduplicate against.
        # PLS-90: when the upstream supplies a stable per-alert id
        # (``source_id`` — e.g. Alertmanager's `fingerprint`), dedup on
        # ``(source, source_id)`` so retries of the SAME alert collapse to one
        # item even if the computed-fingerprint label_fields drift between
        # firings (the cause of 479 items for ~84 unique alerts). This also
        # mirrors the resolve path, which already matches on (source, source_id).
        # Fall back to the computed-fingerprint match when no source_id is given.
        active_statuses = ['firing', 'acknowledged']
        source_id = data.get('source_id')
        if source_id:
            existing = Item.objects.select_for_update().filter(
                mode='alert',
                source=source,
                source_id=source_id,
                status__in=active_statuses,
            ).first()
        else:
            existing = Item.objects.select_for_update().filter(
                mode='alert',
                fingerprint=fingerprint,
                status__in=active_statuses,
            ).first()

        if existing:
            # Deduplicate: update existing alert
            return cls._update_existing_alert(existing, data), False

        # PLAT-944: phantom-replay guard. The active-only dedup above (firing/
        # acknowledged) misses a re-fire of an alert whose original instance has
        # since RESOLVED. So when Alertmanager re-fires its PERSISTED historical
        # alerts after a restart/state-restore — with the IDENTICAL ``startsAt`` as
        # the original — each replay falls through to create a NEW alert + DM (the
        # 2026-06-20 phantom flood: 40+ alerts all stamped startsAt=...T19:21:44.696Z).
        # A GENUINE re-fire carries a NEW startsAt; a REPLAY carries the same one. So
        # if an alert with this (source, source_id|fingerprint) was EVER ingested with
        # the SAME startsAt (any status, incl. resolved), treat it as a replay: dedup
        # against the prior instance and emit no new notification.
        starts_at = (payload or {}).get('starts_at')
        if starts_at:
            replay_q = Item.objects.filter(mode='alert', source=source, payload__starts_at=starts_at)
            replay_q = replay_q.filter(source_id=source_id) if source_id else replay_q.filter(fingerprint=fingerprint)
            replayed = replay_q.order_by('-created_at').first()
            if replayed:
                logger.info(
                    "PLAT-944: skipping phantom-replayed alert (source=%s id=%s startsAt=%s; "
                    "prior item=%s status=%s)",
                    source, source_id or fingerprint, starts_at, replayed.id, replayed.status,
                )
                return replayed, False

        # New alert - create it
        return cls._create_new_alert(data, fingerprint, config), True

    @classmethod
    def _update_existing_alert(cls, existing: Item, data: dict) -> Item:
        """
        Update an existing alert with new occurrence.
        """
        existing.occurrence_count += 1
        existing.last_fired_at = timezone.now()

        # Update severity if new one is higher
        new_severity = data.get('severity')
        if new_severity:
            severity_rank = {'info': 1, 'warning': 2, 'error': 3, 'critical': 4}
            if severity_rank.get(new_severity, 0) > severity_rank.get(existing.severity, 0):
                existing.severity = new_severity
                logger.info(f"Alert {existing.id} severity bumped to {new_severity}")

        # Merge labels and payload (new values override)
        if data.get('labels'):
            existing.labels = {**existing.labels, **data['labels']}

        if data.get('payload'):
            existing.payload = {**existing.payload, **data['payload']}

        # Update description if provided
        if data.get('description'):
            existing.description = data['description']

        existing.save()
        cls._ensure_concrete_assignee(existing)
        cls.rebind_active_oncall()

        logger.info(
            f"Alert deduplicated: {existing.id} ({existing.title}) - "
            f"occurrence #{existing.occurrence_count}"
        )

        return existing

    @classmethod
    def _alert_organization_id(cls, alert: Item) -> Optional[int]:
        """Exact-org boundary for a firing alert.

        ``routing_candidate_snapshot_for_item`` returns
        ``project_organization_missing`` when ``project_id`` is null, so
        pulse-ops-anomaly episodes (no project) never resolved a concrete
        on-call and stayed invisible to every ``pulse_get_my_alerts``.
        """
        project = getattr(alert, 'project', None)
        if project is None and getattr(alert, 'project_id', None):
            project = Project.objects.filter(pk=alert.project_id).first()
        org_id = getattr(project, 'org_id', None) if project is not None else None
        if org_id:
            return int(org_id)
        labels = getattr(alert, 'labels', None) or {}
        for key in ('organization_id', 'org_id'):
            raw = labels.get(key)
            if raw in (None, ''):
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        slug = (
            str(labels.get('organization_slug') or labels.get('org_slug') or '').strip().lower()
            or os.environ.get('SHIZUHA_ORG_SLUG', 'shizuha').strip().lower()
        )
        group = (getattr(alert, 'assignment_group', None) or '').strip().lower()
        from tasks.models import TeamRoster
        qs = TeamRoster.objects.filter(is_active=True).exclude(organization_id__isnull=True)
        if group:
            row = qs.filter(team_name=group, organization_slug=slug).first()
            if row is not None:
                return int(row.organization_id)
        row = qs.filter(organization_slug=slug).first()
        if row is not None:
            return int(row.organization_id)
        env_org = os.environ.get('SHIZUHA_ORGANIZATION_ID', '').strip()
        if env_org.isdigit():
            return int(env_org)
        if slug == 'shizuha':
            return 1
        return None

    @classmethod
    def _resolver_kwargs_for_alert(cls, alert: Item) -> dict:
        """Kwargs that resolve a live on-call seat for a mode=alert episode."""
        from .workflow_service import WorkflowService

        kwargs = {
            'enforce_cap': False,
            'require_live_evidence': True,
            'allow_wakeable': False,
            'project_key': WorkflowService._item_project_key(alert),
        }
        if alert.project_id:
            kwargs['item'] = alert
            return kwargs
        org_id = cls._alert_organization_id(alert)
        kwargs['item'] = None
        if org_id:
            kwargs['organization_id'] = org_id
        return kwargs

    @classmethod
    def _assignee_is_live_oncall(cls, email: str) -> bool:
        if not email:
            return False
        from .workflow_service import WorkflowService
        # Fail-open only on a total health-map outage (PLAT-319). An explicit
        # False (hibernated / disabled / provider-down) must rebind.
        return bool(WorkflowService._is_agent_available_for_assignment(email))

    @classmethod
    def _ensure_concrete_assignee(cls, alert: Item) -> None:
        """Bind a project/team alert to the current available on-call member.

        The team remains the durable owner.  The concrete assignee is the
        heartbeat/DM reconciliation target, and alert routing intentionally
        bypasses task WIP caps: an incident must not sit group-only merely
        because the responder already owns normal task work.

        Hibernated/wakeable seats are not on-call for firing incidents: they
        never run ``pulse_get_my_alerts``. Re-resolve when the bound seat is
        no longer resident-live.
        """
        cls._promote_incident_priority(alert)
        if not alert.assignment_group:
            return
        if alert.assignee_email and cls._assignee_is_live_oncall(alert.assignee_email):
            return
        try:
            from .workflow_service import WorkflowService

            kwargs = cls._resolver_kwargs_for_alert(alert)
            if alert.assignee_email:
                kwargs['exclude_email'] = alert.assignee_email
            uid, email = WorkflowService._resolve_team_assignee(
                alert.assignment_group,
                **kwargs,
            )
            if not email:
                return
            update_fields: list[str] = []
            WorkflowService._apply_resolved_assignee(
                alert,
                uid,
                email,
                update_fields,
            )
            if update_fields:
                alert.save(update_fields=update_fields)
        except Exception:
            # Alert acceptance is fail-open to a durable group-owned episode.
            # Routing failures are independently visible through the existing
            # routability-hold telemetry and must not discard the alert itself.
            logger.exception(
                "Alert %s could not resolve concrete on-call for team %s; "
                "leaving it group-owned",
                alert.id,
                alert.assignment_group,
            )

    @classmethod
    def rebind_active_oncall(cls, *, limit: int = 50) -> int:
        """Re-bind firing/acked alerts whose inbox is empty or not live.

        Cheap: the active alert set is small. Invoked from alert write so a
        live storm also heals siblings (hibernated origin-ci assignee, etc.)
        without a polling sweep.
        """
        rebound = 0
        qs = (
            Item.objects.filter(mode='alert', status__in=['firing', 'acknowledged'])
            .select_related('project')
            .order_by('id')
        )
        for alert in qs[: max(1, int(limit or 50))]:
            before = (alert.assignee_id, (alert.assignee_email or '').strip().lower())
            cls._ensure_concrete_assignee(alert)
            after = (alert.assignee_id, (alert.assignee_email or '').strip().lower())
            if after != before and after[1]:
                rebound += 1
        return rebound

    @classmethod
    def _promote_incident_priority(cls, alert: Item) -> None:
        """Origin CI episodes must tie-break above ordinary high task WIP.

        Heartbeats let alerts win priority ties, but a high alert loses to an
        urgent task. Origin CI failures sat on nagi while four urgent tasks
        occupied the seat.
        """
        if getattr(alert, 'source', None) != 'origin-ci':
            return
        if (alert.priority or '') == 'urgent':
            return
        alert.priority = 'urgent'
        if alert.pk:
            alert.save(update_fields=['priority'])

    @classmethod
    def _mint_alert_item_key(cls, project_id) -> Optional[str]:
        if not project_id:
            return None
        project = Project.objects.filter(pk=project_id).first()
        if project is None:
            return None
        return project.get_next_item_key()

    @classmethod
    def _create_new_alert(cls, data: dict, fingerprint: str, config: dict) -> Item:
        """
        Create a new alert.
        """
        now = timezone.now()
        item_type = data.get('item_type', 'alert.error')
        if not item_type.startswith('alert.'):
            item_type = f'alert.{item_type}'

        assignee_id = data.get('assignee_id')
        assignee_email = data.get('assignee_email')
        if assignee_id or assignee_email:
            assignee_id, assignee_email = Item._resolve_user_ref({
                'id': assignee_id,
                'email': assignee_email,
            })

        source = data['source']
        priority = data.get('priority') or 'normal'
        if source == 'origin-ci':
            priority = 'urgent'

        alert = Item.objects.create(
            mode='alert',
            item_type=item_type,
            title=data['title'],
            description=data.get('description', ''),
            source=source,
            source_id=data.get('source_id'),
            source_url=data.get('source_url'),
            status='firing',
            severity=data.get('severity', 'warning'),
            priority=priority,
            labels=data.get('labels', {}),
            payload=data.get('payload', {}),
            fingerprint=fingerprint,
            fingerprint_fields=config.get('label_fields', []),
            occurrence_count=1,
            first_fired_at=now,
            last_fired_at=now,
            due_date=data.get('due_date'),
            correlation_id=data.get('correlation_id'),
            project_id=data.get('project_id'),
            item_key=cls._mint_alert_item_key(data.get('project_id')),
            assignment_group=data.get('assignment_group'),
            assignee_id=assignee_id,
            assignee_email=assignee_email,
        )
        cls._ensure_concrete_assignee(alert)
        cls.rebind_active_oncall()

        logger.info(f"New alert created: {alert.id} ({alert.title}) - fingerprint: {fingerprint}")

        return alert

    @classmethod
    def handle_recovery(cls, data: dict) -> list[Item]:
        """
        Process a recovery alert and resolve matching firing alerts.

        Args:
            data: Recovery data including:
                - source (required): Source identifier
                - correlation_id (optional): Resolve alerts with this correlation_id
                - resolve_matchers (optional): Label matchers to find alerts to resolve
                - labels (optional): Labels for label-based matching

        Returns:
            List of resolved alerts
        """
        resolved = []
        now = timezone.now()
        resolved_by = f"recovery:{data.get('source', 'unknown')}"

        # Option 1: Explicit correlation_id
        if correlation_id := data.get('correlation_id'):
            alerts = Item.objects.filter(
                mode='alert',
                correlation_id=correlation_id,
                status__in=['firing', 'acknowledged']
            )
            for alert in alerts:
                alert.status = 'resolved'
                alert.resolved_at = now
                alert.resolved_by = resolved_by
                alert.resolution_note = data.get('resolution_note', 'Auto-resolved by recovery alert')
                alert.save()
                resolved.append(alert)
                logger.info(f"Alert {alert.id} resolved by recovery (correlation_id={correlation_id})")

        # Option 2: Match by labels
        elif resolve_matchers := data.get('resolve_matchers'):
            alerts = cls._find_alerts_by_labels(resolve_matchers, data.get('source'))
            for alert in alerts:
                alert.status = 'resolved'
                alert.resolved_at = now
                alert.resolved_by = resolved_by
                alert.resolution_note = data.get('resolution_note', 'Auto-resolved by recovery alert')
                alert.save()
                resolved.append(alert)
                logger.info(f"Alert {alert.id} resolved by recovery (label match)")

        return resolved

    @classmethod
    def _find_alerts_by_labels(cls, matchers: dict, source: str = None) -> list[Item]:
        """
        Find firing alerts matching the given label matchers.
        """
        queryset = Item.objects.filter(
            mode='alert',
            status__in=['firing', 'acknowledged']
        )

        if source:
            queryset = queryset.filter(source=source)

        # Filter by labels using JSON contains
        # Note: This is a simple implementation. For complex matching,
        # consider using django-jsonfield-backport or raw SQL
        matching_alerts = []
        for alert in queryset:
            if all(
                alert.labels.get(key) == value
                for key, value in matchers.items()
            ):
                matching_alerts.append(alert)

        return matching_alerts

    @classmethod
    def auto_resolve_expired(cls) -> list[Item]:
        """
        Auto-resolve alerts that have passed their due_date.
        Should be called periodically by a Celery task.
        """
        now = timezone.now()
        expired_alerts = Item.objects.filter(
            mode='alert',
            status__in=['firing', 'acknowledged'],
            due_date__lt=now
        )

        resolved = []
        for alert in expired_alerts:
            alert.status = 'resolved'
            alert.resolved_at = now
            alert.resolved_by = 'auto:expired'
            alert.resolution_note = 'Auto-resolved due to expiration'
            alert.save()
            resolved.append(alert)
            logger.info(f"Alert {alert.id} auto-resolved (expired)")

        return resolved
