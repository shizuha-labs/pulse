from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from tasks.models import Task

class Command(BaseCommand):
    help = 'Creates a quick reminder task'

    def add_arguments(self, parser):
        parser.add_argument('title', type=str, help='Title of the task')
        parser.add_argument('seconds', type=int, help='Seconds until reminder')
        parser.add_argument('--description', type=str, help='Task description', default='')

    def handle(self, *args, **options):
        reminder_time = timezone.now() + timedelta(seconds=options['seconds'])
        task = Task.objects.create(
            title=options['title'],
            description=options['description'],
            reminder_time=reminder_time,
            priority='medium'
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully created reminder "{task.title}" for {reminder_time}'
            )
        )