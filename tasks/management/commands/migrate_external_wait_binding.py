"""Migrate the one reviewed ADR-0064 carrier; dry-run unless --apply."""

from django.core.management.base import BaseCommand, CommandError

from tasks.models import ExternalDependencyBinding, Item
from tasks.services.external_wait import bind_cross_org_items_privileged


ALLOWED_PAIR = (
    'BKSH-16',
    'PLAT-5503',
    ExternalDependencyBinding.POLICY_TERMINAL,
)


class Command(BaseCommand):
    help = (
        'Dry-run/apply the reviewed BKSH-16 -> PLAT-5503 external-wait binding. '
        'No inference from prose, comments, labels, or key-shaped strings.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--consumer', default='BKSH-16')
        parser.add_argument('--provider', default='PLAT-5503')
        parser.add_argument(
            '--release-policy',
            default=ExternalDependencyBinding.POLICY_TERMINAL,
        )
        parser.add_argument('--consumer-org-id', type=int, required=True)
        parser.add_argument('--provider-org-id', type=int, required=True)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        requested = (
            options['consumer'].strip().upper(),
            options['provider'].strip().upper(),
            options['release_policy'].strip(),
        )
        if requested != ALLOWED_PAIR:
            raise CommandError(
                'Pair is not in the reviewed allowlist; add a separately reviewed '
                'exact tuple before migrating it.'
            )
        try:
            consumer = Item.objects.select_related('project').get(
                item_key=requested[0],
                project__org_id=options['consumer_org_id'],
            )
            provider = Item.objects.select_related('project').get(
                item_key=requested[1],
                project__org_id=options['provider_org_id'],
            )
        except (Item.DoesNotExist, Item.MultipleObjectsReturned) as exc:
            raise CommandError('The exact allowlisted carrier/provider pair is absent.') from exc
        actual = (
            int(consumer.project.org_id),
            consumer.item_key,
            int(provider.project.org_id),
            provider.item_key,
            requested[2],
        )
        expected = (
            options['consumer_org_id'],
            requested[0],
            options['provider_org_id'],
            requested[1],
            requested[2],
        )
        if actual != expected or actual[0] == actual[2]:
            raise CommandError(
                f'Exact tuple mismatch: observed {actual}; refusing without mutation.'
            )
        existing = ExternalDependencyBinding.objects.filter(
            consumer_item=consumer,
            provider_item=provider,
            state__in={
                ExternalDependencyBinding.STATE_ACTIVE,
                ExternalDependencyBinding.STATE_RELEASED,
            },
        ).first()
        if not options['apply']:
            self.stdout.write(
                self.style.WARNING(
                    f'DRY RUN: exact tuple {actual}; '
                    f'existing_receipt={existing.receipt_id if existing else "none"}'
                )
            )
            return
        result = bind_cross_org_items_privileged(
            consumer,
            provider,
            release_policy=requested[2],
            local_display_reference='Global capability delivery',
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'Applied exact tuple {actual}; receipt={result["receipt"]}; '
                f'replayed={result["replayed"]}'
            )
        )
