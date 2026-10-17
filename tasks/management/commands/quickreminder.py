from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from tasks.models import Task
from tasks.user_service import user_service
from django.conf import settings
from kombu import Connection
from taskmanager.celery import app
import os

# AI User ID - consistent across the system
AI_ID = int(os.getenv("AI_ID", "2"))
AI_EMAIL = os.getenv("AI_EMAIL", "ai@shizuha.com")

class Command(BaseCommand):
    help = 'Creates a quick reminder task assigned to a specific user'

    def add_arguments(self, parser):
        parser.add_argument('title', type=str, help='Title of the task')
        parser.add_argument('seconds', type=int, help='Seconds until reminder')
        parser.add_argument('--description', type=str, help='Task description', default='')
        parser.add_argument('--assignee-id', type=int, help='User ID to assign the task to', default=17)
        parser.add_argument('--priority', type=str, choices=['low', 'medium', 'high', 'urgent'], 
                          help='Task priority', default='high')

    def check_broker_connection(self):
        try:
            with Connection(app.conf.broker_url) as conn:
                conn.connect()
                return True
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Broker connection error: {str(e)}"))
            return False

    def handle(self, *args, **options):
        # First check broker connection
        if not self.check_broker_connection():
            self.stdout.write(
                self.style.WARNING(
                    'Warning: Broker connection failed. Please ensure Redis is running and Celery worker is active.'
                )
            )
        
        assignee_id = options['assignee_id']
        
        # Validate and get user information
        self.stdout.write(f'Looking up user with ID: {assignee_id}')
        user_info = user_service.resolve_user_info(user_id=assignee_id)
        
        if not user_info:
            self.stdout.write(
                self.style.ERROR(f'User with ID {assignee_id} not found. Task will be created without assignee.')
            )
            assignee_email = None
        else:
            assignee_email = user_info.get('email')
            self.stdout.write(
                self.style.SUCCESS(f'Found user: {user_info.get("full_name", user_info.get("username"))} ({assignee_email})')
            )
        
        reminder_time = timezone.now() + timedelta(seconds=options['seconds'])
        due_time = reminder_time + timedelta(minutes=30)  # Due 30 minutes after reminder
        
        # Create task with enhanced fields
        task = Task.objects.create(
            title=options['title'],
            description=options['description'],
            task_type='reminder',
            source='manual',
            created_by_id=AI_ID,  # AI creates the task
            created_by_email=AI_EMAIL,
            due_date=due_time,
            reminder_time=reminder_time,
            priority=options['priority'],
            status='pending'
        )
        if user_info:
            task.assign(user_info, save=True)
        
        # Set metadata for the reminder
        task.set_metadata({
            'reminder_type': 'quick_test',
            'created_via': 'management_command',
            'test_reminder': True
        })
        
        # Set notification preferences
        task.set_notification_preferences({
            'email': True,
            'push': True,
            'advance_seconds': options['seconds']
        })
        
        # Set tags
        task.set_tags_list(['quick-reminder', 'test', 'management-command'])
        
        task.save()
        
        self.stdout.write(
            self.style.SUCCESS(f'Created enhanced reminder task with ID: {task.id}')
        )
        self.stdout.write(f'Title: {task.title}')
        self.stdout.write(f'Created by: AI (ID: {AI_ID})')
        self.stdout.write(f'Assignee: {assignee_email or "None"} (ID: {assignee_id if user_info else "None"})')
        self.stdout.write(f'Priority: {task.priority}')
        self.stdout.write(f'Due date: {timezone.localtime(task.due_date)}')
        self.stdout.write(f'Reminder time: {timezone.localtime(task.reminder_time)}')
        self.stdout.write(f'Tags: {task.get_tags_list()}')
        self.stdout.write(f'Metadata: {task.get_metadata()}')
        
        # Reminder scheduling is handled automatically by the post_save signal
        # which adds the item to Redis sorted set timers.
        local_time = timezone.localtime(reminder_time)
        self.stdout.write(
            self.style.SUCCESS(
                f'Timer scheduled via Redis for "{task.title}" at {local_time}'
            )
        )

        # Send immediate assignment notification if assignee exists
        if user_info and getattr(settings, 'TASK_AUTO_NOTIFICATIONS', True):
            try:
                from tasks.serializers import TaskSerializer
                task_data = TaskSerializer(task).data
                if user_service.send_task_notification(task_data):
                    self.stdout.write(
                        self.style.SUCCESS('Immediate assignment notification sent (AI -> User)')
                    )
                else:
                    self.stdout.write(
                        self.style.WARNING('Failed to send immediate notification')
                    )
            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(f'Notification error: {str(e)}')
                )