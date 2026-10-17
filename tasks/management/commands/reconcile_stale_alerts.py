import json
import os
import urllib.parse
import urllib.request

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tasks.models import Item
from tasks.net_utils import safe_urlopen


ACTIVE_STATUSES = ('firing', 'acknowledged')


def _alertmanager_firing_fingerprints(base_url, timeout):
    url = base_url.rstrip('/') + '/api/v2/alerts?' + urllib.parse.urlencode({
        'active': 'true',
        'silenced': 'false',
        'inhibited': 'false',
    })
    req = urllib.request.Request(url, headers={'Accept': 'application/json'})
    with safe_urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    fingerprints = set()
    for alert in payload or []:
        if str(alert.get('status', {}).get('state') or '').lower() != 'active':
            continue
        fp = str(alert.get('fingerprint') or '').strip()
        if fp:
            fingerprints.add(fp)
    return fingerprints


class Command(BaseCommand):
    help = 'Resolve alert-mode Pulse rows whose Alertmanager fingerprint is no longer firing (PLS-219).'

    def add_arguments(self, parser):
        parser.add_argument('--alertmanager-url', default=os.environ.get('ALERTMANAGER_URL', ''))
        parser.add_argument('--source', default='', help='Optional Item.source filter; empty reconciles all Alertmanager-fed sources by fingerprint.')
        parser.add_argument('--timeout', type=float, default=10.0)
        parser.add_argument('--assume-no-live-firing', action='store_true', help='Treat the live firing set as empty; intended only after independently verifying Alertmanager has zero firing alerts.')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int, default=0, help='Optional safety cap for one cleanup batch; 0 = no cap.')

    def handle(self, *args, **opts):
        source = opts['source']
        if opts['assume_no_live_firing']:
            live_fingerprints = set()
        else:
            if not opts['alertmanager_url']:
                raise CommandError('Pass --alertmanager-url or --assume-no-live-firing after verifying Alertmanager has zero firing alerts.')
            live_fingerprints = _alertmanager_firing_fingerprints(opts['alertmanager_url'], opts['timeout'])

        qs = (Item.objects
              .filter(mode='alert', status__in=ACTIVE_STATUSES)
              .exclude(source_id__in=live_fingerprints)
              .order_by('id'))
        if source:
            qs = qs.filter(source=source)
        total = qs.count()
        limit = max(0, opts['limit'] or 0)
        rows = list(qs[:limit] if limit else qs)

        self.stdout.write(
            f'PLS-219 stale alert reconcile: live_firing={len(live_fingerprints)} '
            f'candidates={total} selected={len(rows)} dry_run={opts["dry_run"]}'
        )
        if opts['dry_run']:
            for item in rows[:20]:
                self.stdout.write(f'would resolve {item.item_key or item.id} source_id={item.source_id} status={item.status}')
            return

        with transaction.atomic():
            for item in rows:
                item.resolve(
                    resolved_by='reconcile:pls-219',
                    note='Resolved by PLS-219 stale-alert reconciliation: fingerprint no longer firing in Alertmanager.',
                )
        self.stdout.write(f'Resolved {len(rows)} stale alert row(s).')
