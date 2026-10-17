"""Fail closed when a required exact-org routing seat is absent."""
import json

from django.core.management.base import BaseCommand, CommandError

from tasks.services.team_roster_drift import read_team_roster_drift_signals


class Command(BaseCommand):
    help = (
        'Compare pinned expected seats with exact-org TeamRoster rows and fail '
        'when the live team_roster_compat authority is missing a required seat.'
    )

    def handle(self, *args, **options):
        drifted = []
        for signal in read_team_roster_drift_signals():
            payload = signal.as_dict()
            self.stdout.write(json.dumps(payload, sort_keys=True))
            if signal.drifted:
                drifted.append(payload)

        if drifted:
            raise CommandError(
                'team_roster_drift: '
                + json.dumps(drifted, sort_keys=True, separators=(',', ':'))
            )

        self.stdout.write(self.style.SUCCESS(
            'No required-seat TeamRoster drift detected.'
        ))
