"""PLS-1061: record the fleet-wide agent-runtime digest (rollout-window producer).

The lane-health rollout-window classification reads the fleet agent-runtime
digest signal from cache (``pulse:lane_health:fleet_runtime_digest``). This
command is the **producer** for that signal: a deploy/Hive reconciler (or a
beat/cron hook) calls it when a fleet-wide agent-runtime bump lands, so a lane
with zero routable seats during pod boot is classified ``rollout_window``
instead of a raw lane-stall.

``changed_at`` is only refreshed when the digest actually changes, so the
rollout window starts at the bump, not on every reconcile tick.

Usage:
    python manage.py record_fleet_runtime_digest <digest>
    python manage.py record_fleet_runtime_digest 8878023b
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from tasks.services.lane_health import record_fleet_runtime_digest


class Command(BaseCommand):
    help = (
        'Record the fleet-wide agent-runtime digest (rollout-window producer). '
        'Call when a fleet-wide agent-runtime bump lands.'
    )

    def add_arguments(self, parser):
        parser.add_argument('digest', nargs='?', default='', help='Agent-runtime image digest')

    def handle(self, *args, **options):
        digest = (options.get('digest') or '').strip()
        if not digest:
            raise CommandError('digest is required')
        signal = record_fleet_runtime_digest(digest)
        if signal is None:
            raise CommandError('failed to record fleet runtime digest')
        self.stdout.write(
            f"recorded fleet runtime digest {signal['digest']} "
            f"(changed_at={signal['changed_at']})"
        )
