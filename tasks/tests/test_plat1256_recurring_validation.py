from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.test import TestCase
from tasks.models import Item
from tasks.serializers import ItemSerializer


class RecurringConfigValidationTests(TestCase):
    def test_model_rejects_frequency_outside_choices(self):
        with self.assertRaises(ValidationError) as ctx:
            Item.objects.create(
                title='bad recurring frequency',
                is_recurring=True,
                frequency='every 8 hours',
            )

        msg = str(ctx.exception)
        self.assertIn('Unsupported recurring frequency', msg)
        self.assertIn('frequency=custom', msg)
        self.assertIn('cron strings are not supported', msg)

    def test_model_rejects_custom_cron_pattern(self):
        with self.assertRaises(ValidationError) as ctx:
            Item.objects.create(
                title='cron recurring task',
                is_recurring=True,
                frequency='custom',
                recurrence_pattern='0 */8 * * *',
            )

        self.assertIn('every N hours', str(ctx.exception))
        self.assertIn('cron strings are not supported', str(ctx.exception))

    def test_serializer_accepts_schedulable_custom_text_pattern(self):
        serializer = ItemSerializer(data={
            'title': 'every eight hours',
            'workflow_name': 'simple',
            'is_recurring': True,
            'frequency': 'custom',
            'recurrence_pattern': 'every 8 hours',
            # PLAT-9100 filing contract (carried live again by PLAT-9427's
            # create() consolidation): a teamless ownerless task create
            # loud-fails — name the owning team.
            'assignment_group': 'engineering',
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        item = serializer.save()
        self.assertTrue(item.is_recurring)
        self.assertEqual(item.frequency, 'custom')

    def test_serializer_rejects_partial_update_to_unschedulable_custom_pattern(self):
        item = Item.objects.create(
            title='valid recurring task',
            workflow_name='simple',
            is_recurring=True,
            frequency='custom',
            recurrence_pattern='every 4 hours',
        )
        serializer = ItemSerializer(
            item,
            data={'recurrence_pattern': '*/15 * * * *'},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('frequency', serializer.errors)
        self.assertIn('cron strings are not supported', str(serializer.errors['frequency']))

    def test_backfill_check_reports_rows_inserted_outside_save(self):
        Item.objects.bulk_create([
            Item(
                title='bulk inserted invalid recurring task',
                workflow_name='simple',
                is_recurring=True,
                frequency='custom',
                recurrence_pattern='0 */8 * * *',
            ),
        ])

        stderr = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command('check_recurring_configs', stderr=stderr)

        self.assertIn('Invalid recurring configs found', str(ctx.exception))
        self.assertIn('bulk inserted invalid recurring task', stderr.getvalue())
        self.assertIn('cron strings are not supported', stderr.getvalue())
