# Add missing fields for the unified Item model

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0004_rename_task_to_item'),
    ]

    operations = [
        # Add source_id field
        migrations.AddField(
            model_name='item',
            name='source_id',
            field=models.CharField(blank=True, help_text='External reference ID', max_length=255, null=True),
        ),
        # Add source_url field
        migrations.AddField(
            model_name='item',
            name='source_url',
            field=models.URLField(blank=True, help_text='Link to source system', null=True),
        ),
        # Alter source field to have larger max_length
        migrations.AlterField(
            model_name='item',
            name='source',
            field=models.CharField(default='manual', help_text='Origin: manual, google_calendar, arbiter-backend, etc.', max_length=100),
        ),
        # Remove old parent_task_id IntegerField and add proper parent_item ForeignKey
        # SQLite cannot DROP a column that still has an index; remove the index first.
        migrations.RemoveIndex(
            model_name='item',
            name='tasks_task_parent__a05e1a_idx',
        ),
        migrations.RemoveField(
            model_name='item',
            name='parent_task_id',
        ),
        migrations.AddField(
            model_name='item',
            name='parent_item',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='child_items', to='tasks.item'),
        ),
        # Add index for source + source_id
        migrations.AddIndex(
            model_name='item',
            index=models.Index(fields=['source', 'source_id'], name='tasks_task_source_648a9e_idx'),
        ),
        # Add index for priority
        migrations.AddIndex(
            model_name='item',
            index=models.Index(fields=['priority'], name='tasks_task_priorit_a900d4_idx'),
        ),
    ]
