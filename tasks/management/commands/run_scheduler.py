"""
Django management command to run the Redis sorted set timer scheduler.

Usage:
    python manage.py run_scheduler
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run the Redis sorted set timer scheduler for notifications'

    def handle(self, *args, **options):
        from tasks.scheduler import SchedulerLoop

        self.stdout.write(self.style.SUCCESS('Starting Pulse timer scheduler...'))
        loop = SchedulerLoop()
        loop.start()
        self.stdout.write(self.style.SUCCESS('Scheduler stopped.'))
