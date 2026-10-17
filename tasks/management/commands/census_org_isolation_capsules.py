"""PLS-842 Gen-1 dry-run census (BKSD-14 §7.1).

Read-only (Gen-1). Enumerates 0106 org-safe condition capsules and their outbound
blocks edges. Architecture freezes dispositions from this report before any
Gen-2 mutation.

Usage:
    python manage.py census_org_isolation_capsules
    python manage.py census_org_isolation_capsules --json
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from tasks.services.org_isolation_capsule_census import run_census


class Command(BaseCommand):
    help = (
        'PLS-842 Gen-1 dry-run census of 0106 org-isolation capsules '
        '(read-only; no unlinks or status changes).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--json',
            action='store_true',
            help='Emit the full structured report as JSON.',
        )

    def handle(self, *args, **options):
        report = run_census()
        if options['json']:
            self.stdout.write(json.dumps(report, indent=2, default=str))
            return

        self.stdout.write(
            f"PLS-842 census  schema={report['schema']}  "
            f"census_valid={report['census_valid']}  "
            f"capsules={report['capsule_count']}  edges={report['edge_count']}"
        )
        self.stdout.write(f"report_digest={report['report_digest']}")
        self.stdout.write(
            f"PLAT-4813→PLAT-4720 absent={report['plat4813_plat4720_absent']}"
        )
        for cap in report['capsules']:
            self.stdout.write(
                f"  capsule {cap['item_key']} status={cap['status']} "
                f"disposition={cap['provisional_disposition']} "
                f"digest={cap['capsule_digest'][:12]}…"
            )
        for edge in report['edges']:
            self.stdout.write(
                f"  edge {edge['capsule_key']} -[{edge['link_type']}]-> "
                f"{edge['dependent_key']} same_org={edge['same_org']} "
                f"decision={edge['provisional_decision']}"
            )
        if not report['census_valid']:
            self.stderr.write(self.style.ERROR('census_valid=false'))
            raise SystemExit(2)
