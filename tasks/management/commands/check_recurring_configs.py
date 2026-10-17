from django.core.management.base import BaseCommand, CommandError

from tasks.models import Item
from tasks.services.recurring import validate_recurring_configuration


class Command(BaseCommand):
    help = "Validate existing recurring Item rows so unschedulable configs are fixed before deploy."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-fail-on-invalid",
            dest="fail_on_invalid",
            action="store_false",
            default=True,
            help="Report invalid recurring rows but exit zero.",
        )

    def handle(self, *args, **options):
        invalid = []
        queryset = (
            Item.objects
            .filter(is_recurring=True)
            .only('id', 'item_key', 'title', 'frequency', 'recurrence_pattern')
            .order_by('item_key', 'id')
        )
        for item in queryset.iterator():
            try:
                validate_recurring_configuration(
                    item.is_recurring, item.frequency, item.recurrence_pattern
                )
            except ValueError as exc:
                invalid.append((item, str(exc)))

        if invalid:
            self.stderr.write(f"Found {len(invalid)} invalid recurring item(s):")
            for item, reason in invalid:
                key = item.item_key or f"#{item.pk}"
                self.stderr.write(
                    f"- {key}: {item.title!r} frequency={item.frequency!r} "
                    f"recurrence_pattern={item.recurrence_pattern!r}: {reason}"
                )
            if options.get('fail_on_invalid', True):
                raise CommandError(
                    "Invalid recurring configs found; update them to supported "
                    "frequency values, or use frequency=custom with "
                    "recurrence_pattern=\"every N hours\". Cron strings are not supported."
                )
            return

        self.stdout.write(self.style.SUCCESS("All recurring item configs are schedulable."))
