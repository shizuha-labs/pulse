"""PLS-842 / BKSD-14 §7.2 — Gen-2 frozen-manifest admission (apply path).

Reads a Gen-1 frozen census report (dry-run JSON) and idempotently admits every
capsule classified ``external_unverifiable_locally`` into non-actionable
``waiting_external`` via the typed ``migration_capsule_v1`` authority — the
missing Gen-2 mutation found by focused QA (PLS-916 / PLS-871).

Invariants (BKSD-14 §7.2):
- A capsule whose evidence cannot be produced locally is parked OUT of ready
  WIP / focus / default queues and must not be presented as ordinary work.
- The operation is idempotent (CAS on the frozen capsule digest): a capsule
  already parked, or whose live digest no longer matches the frozen manifest,
  is reported as `skipped` and never mutated.
- The valid PLAT-4812 → PLAT-4002 blocker edge is retained; this command only
  parks the capsule — it never touches edges.

Usage:
    python manage.py census_org_isolation_capsules --json > /tmp/frozen.json
    # Architecture freezes/reviews the report, then:
    python manage.py apply_capsule_frozen_manifest /tmp/frozen.json --dry-run
    python manage.py apply_capsule_frozen_manifest /tmp/frozen.json
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from tasks.models import Item
from tasks.services.external_wait import admit_migration_capsule_wait
from tasks.services.org_isolation_capsule_census import (
    _stable_digest,
    run_census,
)


def apply_frozen_manifest(
    manifest: dict,
    *,
    dry_run: bool = False,
    reviewer_email: str = '',
) -> dict:
    """Apply a frozen census manifest to external_unverifiable capsules.

    Returns counts:
      - ``admitted`` (entered waiting_external via migration_capsule_v1)
      - ``skipped`` (already parked, digest mismatch, or reclassified)
      - ``not_found`` (item_key in manifest not present live)
      - ``failed`` (admission raised a validation error)
    """
    schema = manifest.get('schema')
    if schema != 'pls842.org_isolation_capsule_census.v1':
        raise CommandError(f'unsupported manifest schema: {schema!r}')
    verdicts = {}
    capsules = manifest.get('capsules') or []
    for entry in capsules:
        if entry.get('provisional_disposition') != 'external_unverifiable_locally':
            continue
        item_key = str(entry.get('item_key') or '').strip()
        if not item_key:
            continue
        frozen_digest = str(entry.get('capsule_digest') or '')
        try:
            item = Item.objects.filter(item_key=item_key).first()
            if item is None:
                verdicts.setdefault('not_found', []).append(item_key)
                continue
            # CAS: only admit when the live capsule still matches the frozen
            # manifest. A changed capsule is a new semantic generation and must
            # be re-censused, not silently parked under a stale digest.
            live = [
                c for c in (run_census().get('capsules') or []) if c.get('item_key') == item_key
            ]
            live_digest = live[0].get('capsule_digest') if live else None
            if not live_digest or live_digest != frozen_digest:
                verdicts.setdefault('skipped_digest_mismatch', []).append(
                    (item_key, frozen_digest[:12], (live_digest or '')[:12])
                )
                continue
            if dry_run:
                verdicts.setdefault('would_admit', []).append(item_key)
                continue
            from tasks.models import ExternalWaitAuthority
            already = (
                item.status == 'waiting_external'
                or ExternalWaitAuthority.objects.filter(
                    item=item,
                    branch=ExternalWaitAuthority.BRANCH_MIGRATION_CAPSULE,
                ).exists()
            )
            if already:
                verdicts.setdefault('already_parked', []).append(item_key)
                continue
            admit_migration_capsule_wait(
                item,
                capsule_digest=frozen_digest,
                reason_code='external_unverifiable_locally',
                local_recheck_criterion='Re-check from this organization’s '
                'own evidence when a local evidence path exists.',
                reviewer_email=reviewer_email,
            )
            item.refresh_from_db()
            verdicts.setdefault('admitted', []).append(
                {'item_key': item_key, 'status': item.status}
            )
        except Exception as exc:  # noqa: BLE001 — per-row isolation
            verdicts.setdefault('failed', []).append(
                {'item_key': item_key, 'error': str(exc)[:200]}
            )
    return verdicts


class Command(BaseCommand):
    help = (
        'PLS-842 Gen-2: apply a frozen census manifest, admitting each '
        'external_unverifiable_locally capsule to waiting_external via '
        'migration_capsule_v1 (idempotent; CAS on the capsule digest).'
    )

    def add_arguments(self, parser):
        parser.add_argument('manifest', help='Path to the frozen census JSON file.')
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be admitted without mutating anything.',
        )
        parser.add_argument(
            '--reviewer-email',
            default='',
            help='Reviewer identity recorded on each admission authority.',
        )

    def handle(self, *args, **options):
        path = options['manifest']
        try:
            with open(path, encoding='utf-8') as fh:
                manifest = json.load(fh)
        except (OSError, ValueError) as exc:
            raise CommandError(f'cannot read manifest {path}: {exc}')
        verdicts = apply_frozen_manifest(
            manifest,
            dry_run=options['dry_run'],
            reviewer_email=options['reviewer_email'],
        )
        self.stdout.write(json.dumps(verdicts, indent=2, default=str))
        if options['dry_run']:
            self.stdout.write(
                'dry-run: no mutations performed — pass without --dry-run to apply.',
            )
