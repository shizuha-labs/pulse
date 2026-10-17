from tasks.models import Task
from tasks.tasks import send_reminder

# Reset the first task's reminder_sent flag
task = Task.objects.get(id=1)
task.reminder_sent = False
task.save()
print(f'Reset task {task.id} - {task.title}')
print(f'Reminder sent: {task.reminder_sent}')

# Now manually call the send_reminder task
result = send_reminder(task.id)
print(f'Result: {result}')

# Check if the flag was updated
task.refresh_from_db()
print(f'After processing - Reminder sent: {task.reminder_sent}')