# Rename Task model to Item for Django state management
# The table name remains tasks_task for backward compatibility
# We use AlterModelTable to ensure the table name is explicitly set

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0003_approvalrule_escalationrule_fingerprintconfig_and_more'),
    ]

    operations = [
        # Rename the model in Django's state (not the table)
        migrations.RenameModel(
            old_name='Task',
            new_name='Item',
        ),
        # Ensure the table name stays as tasks_task
        migrations.AlterModelTable(
            name='item',
            table='tasks_task',
        ),
    ]
