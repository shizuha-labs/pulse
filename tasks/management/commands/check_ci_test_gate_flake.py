"""PLS-1043: run the CI test-gate flake probe.

Detects recurring Connect premerge `test-gate` failures on the same head where
the sibling `frontend-gate` is green, classifies them as infra-flake (not code),
and fires a Pulse alert with a re-trigger instruction so reviewers don't issue
false Request-Changes. Scheduled by the Pulse recurring engine / cron.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from tasks.services.ci_flake_probe import check_test_gate_flake


class Command(BaseCommand):
    help = 'Detect recurring Connect premerge test-gate infra-flakes and alert.'

    def handle(self, *args, **options):
        result = check_test_gate_flake()
        self.stdout.write(
            f"ci-flake-probe: checked={result['checked_heads']} "
            f"flakes={len(result['flakes'])} alerts={result['alerts']}"
        )
        for f in result['flakes']:
            self.stdout.write(
                f"  flake pr=#{f['pr_number']} head={f['head_sha'][:12]} "
                f"occurrences={f['occurrences']}"
            )
