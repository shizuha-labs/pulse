from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0088_pls272_objectives'),
    ]

    operations = [
        migrations.AlterField(
            model_name='itemactivity',
            name='action',
            field=models.CharField(
                choices=[
                    ('created', 'Created'),
                    ('updated', 'Updated'),
                    ('status_changed', 'Status Changed'),
                    ('assigned', 'Assigned'),
                    ('unassigned', 'Unassigned'),
                    ('priority_changed', 'Priority Changed'),
                    ('moved', 'Moved to Project'),
                    ('commented', 'Commented'),
                    ('attachment_added', 'Attachment Added'),
                    ('attachment_removed', 'Attachment Removed'),
                    ('linked', 'Linked'),
                    ('unlinked', 'Unlinked'),
                    ('watched', 'Started Watching'),
                    ('unwatched', 'Stopped Watching'),
                    ('completed', 'Completed'),
                    ('reopened', 'Reopened'),
                    ('recurring_cycle_fired', 'Recurring Cycle Fired'),
                    ('recurring_cycle_skipped', 'Recurring Cycle Skipped (still active)'),
                    ('recurring_cycle_stale', 'Recurring Cycle Stale'),
                    ('recurring_cycle_auto_close_failed', 'Recurring Cycle Auto-Close Failed'),
                    ('pr_attached', 'Pull Request Attached'),
                    ('pr_removed', 'Pull Request Removed'),
                    ('pr_status_changed', 'Pull Request Status Changed'),
                ],
                help_text='Type of activity',
                max_length=64,
            ),
        ),
    ]
