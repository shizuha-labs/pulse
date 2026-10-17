import json
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from tasks.models import Item
from tasks.services.external_wait import admit_external_response_hold


class Command(BaseCommand):
    """Allowlisted, dry-run-first branch-3 migration entry point.

    The allowlist is an operator-supplied JSON array of exact tuples. The
    command never infers carriers or evidence from comments, labels, prose, or
    key-shaped strings.
    """

    help = 'Dry-run/apply one exact allowlisted external-response hold tuple.'

    def add_arguments(self, parser):
        parser.add_argument('--carrier-id', type=int, required=True)
        parser.add_argument('--checkpoint-at', required=True)
        parser.add_argument('--source-evidence-item-id', type=int, required=True)
        parser.add_argument('--evidence-kind', required=True)
        parser.add_argument('--reference-kind', required=True)
        parser.add_argument('--reference-id', required=True)
        parser.add_argument('--redacted-label', required=True)
        parser.add_argument('--correlation-key', required=True)
        parser.add_argument('--actor-email', required=True)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **opts):
        checkpoint = parse_datetime(opts['checkpoint_at'])
        if checkpoint is None:
            raise CommandError('checkpoint-at must be ISO-8601')
        try:
            allowlist = json.loads(
                os.environ.get('PULSE_EXTERNAL_RESPONSE_MIGRATION_ALLOWLIST', '[]')
            )
        except json.JSONDecodeError as exc:
            raise CommandError('invalid migration allowlist JSON') from exc
        carrier = Item.objects.select_related('project').get(pk=opts['carrier_id'])
        expected = {
            'org': carrier.project.org_id,
            'carrier_id': carrier.pk,
            'checkpoint': checkpoint.isoformat(),
            'source_evidence': {
                'kind': opts['evidence_kind'],
                'item_id': opts['source_evidence_item_id'],
            },
            'release_policy': 'evidence_or_checkpoint_v1',
        }
        if expected not in allowlist:
            raise CommandError('exact migration tuple is not allowlisted')
        rendered = json.dumps(expected, sort_keys=True)
        if not opts['apply']:
            self.stdout.write(f'DRY RUN: {rendered}')
            return

        actor = get_user_model().objects.filter(
            email__iexact=opts['actor_email']
        ).first()
        if actor is None:
            raise CommandError('named migration actor does not exist')
        result = admit_external_response_hold(
            carrier,
            user=actor,
            checkpoint_at=checkpoint,
            source_evidence=expected['source_evidence'],
            display_reference={
                'kind': opts['reference_kind'],
                'reference_id': opts['reference_id'],
                'redacted_label': opts['redacted_label'],
            },
            correlation_key=opts['correlation_key'],
        )
        self.stdout.write(json.dumps(result, sort_keys=True))
