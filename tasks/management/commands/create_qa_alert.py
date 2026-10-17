"""PLS-817: safe controlled QA alert fixture for Pulse ACK/resolve/silence cadence checks.

Pulse's mandatory F0.7 daily QA recipe requires a current actionable test alert
that can be acknowledged, temporarily silenced, and resolved with auditable
timestamps. This command creates one WITHOUT paging any external/on-call
recipient: the alert is created through the normal dedup path but no
notification task is dispatched, so nothing fires a DM/page.

Safety guarantees:
- source='qa-fixture' + title prefix '[QA FIXTURE]' — clearly labelled, never
  mistaken for a real incident.
- No send_alert_notification call — never pages external/on-call recipients.
- Idempotent: re-running with the same --source-id dedups to the existing
  active alert (returns the same item, is_new=False).
- Terminalizable: --resolve resolves the alert; --cleanup resolves AND deletes
  the QA fixture row so it cannot linger as an actionable alert.

Usage:
  python manage.py create_qa_alert --assignee-email qa@shizuha.com
  python manage.py create_qa_alert --resolve --source-id qa-f0.7-2026-08-20
  python manage.py create_qa_alert --cleanup --source-id qa-f0.7-2026-08-20
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Create a safe, non-paging QA alert fixture for the F0.7 ACK/resolve/silence recipe.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--assignee-email',
            default='qa@shizuha.com',
            help='QA user the fixture alert is assigned to (org-scoped).',
        )
        parser.add_argument(
            '--source-id',
            default=None,
            help='Stable dedup id (default: qa-f0.7-<YYYY-MM-DD>). Re-running with the same id dedups.',
        )
        parser.add_argument(
            '--title',
            default='[QA FIXTURE] Pulse F0.7 alert lifecycle check',
            help='Alert title (must start with [QA FIXTURE] to stay clearly labelled).',
        )
        parser.add_argument(
            '--resolve',
            action='store_true',
            help='Resolve the existing QA fixture alert instead of creating one.',
        )
        parser.add_argument(
            '--cleanup',
            action='store_true',
            help='Resolve AND delete the QA fixture alert (terminal cleanup, no lingering actionable alert).',
        )

    def handle(self, *args, **options):
        from tasks.models import Item
        from tasks.services.deduplication import AlertDeduplicationService

        title = options['title']
        if not title.startswith('[QA FIXTURE]'):
            self.stderr.write('ERROR: title must start with "[QA FIXTURE]" to stay clearly labelled.')
            raise SystemExit(2)

        source_id = options['source_id'] or f"qa-f0.7-{timezone.now():%Y-%m-%d}"
        assignee_email = options['assignee_email']

        if options['resolve'] or options['cleanup']:
            alert = (
                Item.objects.filter(
                    mode='alert',
                    source='qa-fixture',
                    source_id=source_id,
                )
                .order_by('-created_at')
                .first()
            )
            if alert is None:
                self.stderr.write(f'No QA fixture alert found for source_id={source_id}')
                raise SystemExit(1)
            if alert.status not in ('resolved', 'cancelled', 'canceled', 'closed'):
                alert.resolve(resolved_by='qa-fixture', note='PLS-817 QA fixture cleanup')
                self.stdout.write(self.style.SUCCESS(
                    f'Resolved QA fixture alert {alert.id} ({alert.item_key}).'
                ))
            else:
                self.stdout.write(f'QA fixture alert {alert.id} already resolved.')
            if options['cleanup']:
                alert.delete()
                self.stdout.write(self.style.SUCCESS(f'Deleted QA fixture alert {alert.id}.'))
            return

        data = {
            'item_type': 'alert.error',
            'title': title,
            'description': (
                'PLS-817 QA fixture: exercise the real alert lifecycle '
                '(create -> ACK -> temporary silence -> resolve) with auditable '
                'timestamps. Safe: no external/on-call paging. Resolve/clean with '
                '`create_qa_alert --resolve/--cleanup --source-id ...`.'
            ),
            'source': 'qa-fixture',
            'source_id': source_id,
            'severity': 'warning',
            'priority': 'normal',
            'labels': {
                'qa_fixture': 'true',
                'recipe': 'f0.7',
            },
            'payload': {'starts_at': timezone.now().isoformat()},
            'assignee_email': assignee_email,
        }

        alert, is_new = AlertDeduplicationService.submit_alert(data)
        # Deliberately NO send_alert_notification.delay() — QA fixtures never page.
        if is_new:
            self.stdout.write(self.style.SUCCESS(
                f'Created QA fixture alert {alert.id} ({alert.item_key}) '
                f'status={alert.status} assigned to {assignee_email}. '
                f'No notification dispatched (non-paging by design).'
            ))
        else:
            self.stdout.write(
                f'Deduplicated to existing QA fixture alert {alert.id} '
                f'({alert.item_key}) status={alert.status}.'
            )
        self.stdout.write(
            'Lifecycle: ACK via POST /api/items/<id>/acknowledge/, silence via '
            'POST /api/items/<id>/silence/, resolve via POST /api/items/<id>/resolve/ '
            '(or `create_qa_alert --resolve --source-id ' + source_id + '`).'
        )
